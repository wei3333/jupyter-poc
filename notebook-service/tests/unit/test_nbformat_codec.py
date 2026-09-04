"""nbformat_codec 单元测试（指引 §15.2）。"""
from __future__ import annotations

import copy
import re

import pytest

from app.errors import InvalidNotebook
from app.nbformat_codec import (
    canonical_json_bytes,
    compute_content_hash,
    default_notebook,
    normalize_for_create,
    sha256_hex,
    validate_for_save,
)

from tests.conftest import make_code_cell, make_notebook

CELL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _cell_ids(doc):
    return [cell["id"] for cell in doc["cells"]]


# -- 默认 Notebook ----------------------------------------------------------


def test_default_notebook_is_minimal_valid():
    doc = default_notebook()
    assert doc == {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {},
        "cells": [],
    }


def test_default_notebook_does_not_contain_product_content():
    # 产品模板/欢迎语/默认 Cell 属于产品模板层，不进底层服务。
    doc = default_notebook()
    assert doc["cells"] == []
    assert "lumen" not in doc["metadata"]


# -- 创建规范化 ---------------------------------------------------------------


def test_create_without_content_uses_default():
    assert normalize_for_create(default_notebook()) == default_notebook()


def test_create_nbformat_40_missing_ids_gets_ids_and_minor_bump():
    raw = make_notebook(
        [
            {"cell_type": "code", "metadata": {}, "source": "x", "execution_count": None, "outputs": []},
            {"cell_type": "markdown", "metadata": {}, "source": "hi"},
        ],
        minor=4,
    )
    doc = normalize_for_create(raw)
    assert doc["nbformat_minor"] == 5
    ids = _cell_ids(doc)
    assert len(set(ids)) == 2
    for cell_id in ids:
        assert CELL_ID_RE.match(cell_id) and len(cell_id) == 32


def test_create_fixes_duplicate_ids():
    raw = make_notebook(
        [make_code_cell("dup"), make_code_cell("dup"), make_code_cell("dup")]
    )
    doc = normalize_for_create(raw)
    assert len(set(_cell_ids(doc))) == 3


def test_create_fixes_invalid_ids():
    raw = make_notebook(
        [make_code_cell("has space"), make_code_cell("a" * 65), make_code_cell(None)]
    )
    doc = normalize_for_create(raw)
    for cell_id in _cell_ids(doc):
        assert CELL_ID_RE.match(cell_id)


def test_create_keeps_valid_ids_and_bumps_minor():
    raw = make_notebook([make_code_cell("my_cell-1")], minor=4)
    doc = normalize_for_create(raw)
    assert _cell_ids(doc) == ["my_cell-1"]
    assert doc["nbformat_minor"] == 5


def test_create_does_not_modify_input():
    raw = make_notebook([make_code_cell("a1")], minor=4)
    snapshot = copy.deepcopy(raw)
    normalize_for_create(raw)
    assert raw == snapshot


def test_create_rejects_non_4_nbformat():
    raw = make_notebook(minor=5, nbformat=3)
    with pytest.raises(InvalidNotebook) as exc:
        normalize_for_create(raw)
    assert exc.value.details["path"] == "/content/nbformat"


def test_create_rejects_unknown_cell_type():
    raw = make_notebook(
        [{"id": "x", "cell_type": "quantum_circuit", "metadata": {}, "source": []}]
    )
    with pytest.raises(InvalidNotebook):
        normalize_for_create(raw)


def test_create_rejects_invalid_output_type():
    raw = make_notebook(
        [dict(make_code_cell(), outputs=[{"output_type": "update_display_data", "data": {}, "metadata": {}}])]
    )
    with pytest.raises(InvalidNotebook):
        normalize_for_create(raw)


def test_create_preserves_lumen_and_unknown_metadata_and_mime_bundles():
    raw = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "lumen": {"owner": "someone", "title": "t"},
            "custom_std": {"nested": {"x": [1, 2]}},
        },
        "cells": [
            {
                "id": "c1",
                "cell_type": "code",
                "metadata": {"lumen": {"cell_type": "circuit", "circuit": {"qubits": 2}}},
                "source": "H 0",
                "execution_count": 3,
                "outputs": [
                    {
                        "output_type": "display_data",
                        "data": {"text/html": "<b>x</b>", "image/png": "aGVsbG8="},
                        "metadata": {"lumen": {"circuit": "H"}},
                    },
                    {
                        "output_type": "execute_result",
                        "execution_count": 3,
                        "metadata": {},
                        "data": {"application/json": {"a": {"b": [1, 2]}, "text/plain": "x"}},
                    },
                ],
            }
        ],
    }
    doc = normalize_for_create(raw)
    assert doc["metadata"]["lumen"] == {"owner": "someone", "title": "t"}
    assert doc["metadata"]["custom_std"] == {"nested": {"x": [1, 2]}}
    cell = doc["cells"][0]
    assert cell["metadata"]["lumen"]["circuit"] == {"qubits": 2}
    assert cell["outputs"][0]["data"]["image/png"] == "aGVsbG8="
    assert cell["outputs"][1]["data"]["application/json"] == {
        "a": {"b": [1, 2]}, "text/plain": "x"
    }


@pytest.mark.parametrize("source", ["print(1)", ["print(", "1)"]])
def test_source_round_trips_unchanged(source):
    raw = make_notebook([make_code_cell("c1", source=source)])
    doc = normalize_for_create(raw)
    assert doc["cells"][0]["source"] == source


# -- PUT 严格校验 --------------------------------------------------------------


def test_put_rejects_missing_cell_id():
    raw = make_notebook(
        [{"cell_type": "code", "metadata": {}, "source": "x", "execution_count": None, "outputs": []}]
    )
    with pytest.raises(InvalidNotebook) as exc:
        validate_for_save(raw)
    assert exc.value.details["path"] == "/content/cells/0/id"


def test_put_rejects_duplicate_cell_id():
    raw = make_notebook([make_code_cell("a"), make_code_cell("a")])
    with pytest.raises(InvalidNotebook) as exc:
        validate_for_save(raw)
    assert exc.value.details["path"] == "/content/cells/1/id"
    assert "unique" in exc.value.details["reason"]


def test_put_rejects_invalid_cell_id_format():
    raw = make_notebook([make_code_cell("has space")])
    with pytest.raises(InvalidNotebook) as exc:
        validate_for_save(raw)
    assert exc.value.details["path"] == "/content/cells/0/id"


def test_put_rejects_minor_below_5():
    raw = make_notebook([make_code_cell()], minor=4)
    with pytest.raises(InvalidNotebook) as exc:
        validate_for_save(raw)
    assert exc.value.details["path"] == "/content/nbformat_minor"


def test_put_rejects_update_display_data_output():
    raw = make_notebook(
        [dict(make_code_cell(), outputs=[
            {"output_type": "update_display_data", "data": {"text/plain": "x"}, "metadata": {}}
        ])]
    )
    with pytest.raises(InvalidNotebook):
        validate_for_save(raw)


def test_put_rejects_transient_in_output():
    raw = make_notebook(
        [dict(make_code_cell(), outputs=[
            {"output_type": "stream", "name": "stdout", "text": "x", "transient": {"x": 1}}
        ])]
    )
    with pytest.raises(InvalidNotebook):
        validate_for_save(raw)


def test_put_rejects_unknown_output_and_cell_fields():
    raw = make_notebook(
        [dict(make_code_cell(), outputs=[{"output_type": "quantum_state", "data": {}}])]
    )
    with pytest.raises(InvalidNotebook):
        validate_for_save(raw)


def test_put_rejects_nan_and_infinity():
    for bad in [float("nan"), float("inf"), float("-inf")]:
        raw = make_notebook([dict(make_code_cell(), outputs=[
            {"output_type": "execute_result", "execution_count": 1, "metadata": {},
             "data": {"text/plain": bad}}
        ])])
        with pytest.raises(InvalidNotebook):
            validate_for_save(raw)


def test_put_accepts_valid_doc_without_modification():
    raw = make_notebook([make_code_cell("c1"), make_code_cell("c2")])
    doc = validate_for_save(raw)
    assert doc == raw


def test_put_does_not_modify_input():
    raw = make_notebook([make_code_cell("c1")])
    snapshot = copy.deepcopy(raw)
    validate_for_save(raw)
    assert raw == snapshot


def test_put_accepts_string_source():
    raw = make_notebook([make_code_cell("c1", source="print(1)")])
    doc = validate_for_save(raw)
    assert doc["cells"][0]["source"] == "print(1)"


def test_put_accepts_extension_metadata():
    raw = make_notebook(
        [dict(make_code_cell(), metadata={"lumen": {"cell_type": "circuit", "circuit": {"qubits": 3}}})],
        metadata={"lumen": {"a": 1}},
    )
    doc = validate_for_save(raw)
    assert doc["metadata"]["lumen"] == {"a": 1}
    assert doc["cells"][0]["metadata"]["lumen"]["circuit"] == {"qubits": 3}


# -- canonical JSON 与哈希 ------------------------------------------------------


def test_canonical_bytes_are_stable_across_key_order_and_whitespace():
    a = {"nbformat": 4, "cells": [], "metadata": {}}
    b = {"metadata": {}, "cells": [], "nbformat": 4}
    assert canonical_json_bytes(a) == canonical_json_bytes(b)


def test_canonical_bytes_have_no_bom_no_trailing_newline():
    data = canonical_json_bytes({"a": 1})
    assert not data.startswith(b"\xef\xbb\xbf")
    assert not data.endswith(b"\n")


def test_canonical_bytes_preserve_array_order():
    a = canonical_json_bytes({"cells": [1, 2]})
    b = canonical_json_bytes({"cells": [2, 1]})
    assert a != b


def test_canonical_bytes_reject_non_finite_numbers():
    import json as _json

    with pytest.raises(ValueError):
        canonical_json_bytes({"x": float("nan")})
    with pytest.raises(ValueError):
        canonical_json_bytes({"x": float("inf")})


def test_content_hash_format_and_stability():
    doc = {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": []}
    hash_id = compute_content_hash(doc)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", hash_id)
    assert compute_content_hash(doc) == hash_id
    digest = sha256_hex(canonical_json_bytes(doc))
    assert hash_id == f"sha256:{digest}"
