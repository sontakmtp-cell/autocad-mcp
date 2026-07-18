from __future__ import annotations

import json
from pathlib import Path

import pytest

from autocad_mcp.backends.file_ipc import encode_attributes

ROOT = Path(__file__).resolve().parents[1]
PYTHON_BACKEND = ROOT / "src" / "autocad_mcp" / "backends" / "file_ipc.py"
LISP_DISPATCHER = ROOT / "lisp-code" / "mcp_dispatch.lsp"
ATTRIBUTE_TOOLS = ROOT / "lisp-code" / "attribute_tools.lsp"
STARTUP = ROOT / "lisp-code" / "acadltdoc.lsp.example"
SERVER = ROOT / "src" / "autocad_mcp" / "server.py"
ATTRIBUTE_CASES = json.loads(
    (ROOT / "tests" / "fixtures" / "block_attributes.json").read_text(encoding="utf-8")
)["cases"]


def decode_attributes_fixture(text: str) -> list[tuple[str, str]]:
    """Pure-Python mirror of the fixture-friendly AutoLISP decoder."""
    result: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        colon = text.index(":", pos)
        tag_len = int(text[pos:colon])
        pos = colon + 1
        tag = text[pos : pos + tag_len]
        pos += tag_len
        colon = text.index(":", pos)
        value_len = int(text[pos:colon])
        pos = colon + 1
        value = text[pos : pos + value_len]
        pos += value_len
        result.append((tag, value))
    return result


@pytest.mark.parametrize(
    "case",
    ATTRIBUTE_CASES,
    ids=[case["name"] for case in ATTRIBUTE_CASES],
)
def test_attribute_encoding_roundtrip(case):
    attributes = case["attributes"]
    encoded = encode_attributes(attributes)
    assert decode_attributes_fixture(encoded) == list(attributes.items())
    if "expected_error" in case:
        assert case["expected_error"] in LISP_DISPATCHER.read_text(encoding="utf-8")


def test_python_has_no_keyboard_or_focus_fallback():
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "src" / "autocad_mcp").rglob("*.py")
    )
    for forbidden in ("PostMessageW", "WM_CHAR", "VK_ESCAPE", "pyautogui", "clipboard"):
        assert forbidden not in source
    backend_source = PYTHON_BACKEND.read_text(encoding="utf-8")
    assert "PostCommand" in backend_source
    assert "SendCommand" in backend_source
    assert "IsQuiescent" in backend_source


def test_lisp_contains_reliability_guards():
    source = LISP_DISPATCHER.read_text(encoding="utf-8")
    required = (
        "mcp-call-with-sysvars",
        '(("ATTREQ" 0) ("ATTDIA" 0) ("CMDECHO" 0))',
        "mcp-restore-sysvars",
        "mcp-cancel-owned-command before",
        "attributes_str",
        "attribute_tag_not_found",
        "session_id",
        "c:mcp-dispatch-request",
        "acad_strlsort",
        "command_not_completed",
    )
    for marker in required:
        assert marker in source
    assert "(setq *error*" not in source.lower()
    assert '(setvar "FILEDIA" 1)' not in source


@pytest.mark.parametrize("path", [LISP_DISPATCHER, ATTRIBUTE_TOOLS], ids=lambda path: path.name)
def test_lisp_parentheses_are_balanced(path):
    source = path.read_text(encoding="utf-8")
    balance = 0
    in_string = False
    escaped = False
    in_comment = False
    for char in source:
        if in_comment:
            if char == "\n":
                in_comment = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == ";":
            in_comment = True
        elif char == '"':
            in_string = True
        elif char == "(":
            balance += 1
        elif char == ")":
            balance -= 1
            assert balance >= 0
    assert balance == 0
    assert not in_string


def test_attribute_tools_restores_attreq_after_insert_error():
    source = ATTRIBUTE_TOOLS.read_text(encoding="utf-8")
    helper = source.split("(defun c:insert-block-simple", 1)[1].split(
        ";; Update block attributes", 1
    )[0]
    assert "vl-catch-all-apply" in helper
    assert "(list \"ATTREQ\" old-attreq)" in helper


def test_startup_file_is_path_independent_and_dialog_free():
    source = STARTUP.read_text(encoding="utf-8")
    assert '(findfile "mcp_dispatch.lsp")' in source
    assert "(load mcp-dispatch-path)" in source
    assert "APPLOAD" not in source.upper()
    assert "C:/" not in source and "C:\\" not in source


def test_server_uses_real_health_probe():
    source = SERVER.read_text(encoding="utf-8")
    health_branch = source.split('elif operation == "health":', 1)[1].split(
        'elif operation == "runtime":', 1
    )[0]
    assert "result = await backend.health()" in health_branch
    assert "result = await backend.status()" not in health_branch
