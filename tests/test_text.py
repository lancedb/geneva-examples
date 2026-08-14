"""Text workflow: synthetic catalog, the batch embedding UDF, and the view wiring.

Three layers, mirroring how the example is put together:

* ``products.py`` is pure — generated rows and Arrow batches are asserted directly.
* the sentence-transformers UDF is driven against a **fake**
  ``sentence_transformers`` module injected into ``sys.modules``, so the batching,
  null handling and dimension check are really executed without downloading model
  weights in CI (the factory's *binding* is asserted against the real geneva
  decorator).
* the two CLIs are driven through ``CliRunner`` with the cluster faked, which is
  what pins the materialized-view wiring: what the projection handed to
  ``select()`` contains, and that ``refresh`` runs on the created view.
"""

from __future__ import annotations

import sys
import types
from contextlib import nullcontext
from typing import Any, ClassVar

import numpy as np
import pyarrow as pa
import pytest
from _fakes import FakeConn, FakeTable
from click.testing import CliRunner

from geneva_examples.examples import cli
from geneva_examples.examples.text import embed_description as embed_mod
from geneva_examples.examples.text import products as products_mod

# --- synthetic catalog ------------------------------------------------------


def test_generate_products_is_deterministic_per_seed():
    first = products_mod.generate_products(12, seed=7)
    assert first == products_mod.generate_products(12, seed=7)
    # A different seed re-rolls the wording (ids/categories are positional).
    other = products_mod.generate_products(12, seed=8)
    assert [r["description"] for r in first] != [r["description"] for r in other]


def test_generate_products_covers_every_category_round_robin():
    rows = products_mod.generate_products(len(products_mod.CATEGORIES) * 3)
    counts = dict.fromkeys(products_mod.CATEGORIES, 0)
    for row in rows:
        counts[row["category"]] += 1
    # Round-robin assignment: every category appears the same number of times,
    # so even a small catalog has more than one cluster to search across.
    assert set(counts.values()) == {3}


def test_generate_products_word_count_matches_description():
    for row in products_mod.generate_products(20, seed=3):
        assert row["word_count"] == len(row["description"].split())
        # The description mentions the product noun and reads as one sentence pair.
        assert row["description"].endswith(".")


def test_generate_products_titles_keep_apostrophes_and_hyphens_intact():
    titles = {r["title"] for r in products_mod.generate_products(60, seed=1)}
    # str.title() would render these "Chef'S Knife" / "Tri-Ply".
    assert not any("'S" in t for t in titles)
    assert all(t == t.lstrip() and t[:1].isupper() for t in titles)


def test_generate_products_ids_are_unique_and_zero_padded():
    rows = products_mod.generate_products(11)
    ids = [r["product_id"] for r in rows]
    assert len(set(ids)) == len(ids)
    assert ids[0] == "prod-00000" and ids[10] == "prod-00010"


def test_generate_products_rejects_empty_catalog():
    with pytest.raises(ValueError, match="rows must be >= 1"):
        products_mod.generate_products(0)


def test_to_batches_slices_and_types_rows():
    rows = products_mod.generate_products(7)
    batches = products_mod.to_batches(rows, frag_size=3)

    assert [b.num_rows for b in batches] == [3, 3, 1]
    assert all(b.schema == products_mod.SCHEMA for b in batches)
    # No rows lost or reordered by the slicing.
    flat = [r for b in batches for r in b.to_pylist()]
    assert flat == rows


def test_to_batches_floors_frag_size_at_one():
    rows = products_mod.generate_products(3)
    assert [b.num_rows for b in products_mod.to_batches(rows, frag_size=0)] == [1, 1, 1]


# --- fake sentence-transformers --------------------------------------------

FAKE_DIM = 4


class _FakeModel:
    """Stands in for ``SentenceTransformer``: records calls, returns fixed vectors."""

    def __init__(self, model_name: str, device: str | None = None) -> None:
        self.model_name = model_name
        self.device = device or "cpu"
        self.dim = _FakeModel.dim
        self.encoded: list[list[str]] = []
        self.encode_kwargs: dict[str, Any] = {}
        _FakeModel.instances.append(self)

    # Class-level knobs the tests set: every instance reports `dim`, and each
    # construction is recorded so "loaded once per actor" is assertable.
    dim: ClassVar[int] = FAKE_DIM
    instances: ClassVar[list[_FakeModel]] = []

    def get_embedding_dimension(self) -> int:
        return self.dim

    def encode(self, texts, **kwargs):
        self.encoded.append(list(texts))
        self.encode_kwargs = kwargs
        # Distinct, deterministic vector per text so scatter order is checkable.
        return np.array([[float(len(t))] * FAKE_DIM for t in texts], dtype=np.float32)


@pytest.fixture
def fake_st(monkeypatch: pytest.MonkeyPatch) -> type[_FakeModel]:
    """Inject a fake ``sentence_transformers`` module; yield the model class.

    The UDF imports it inside ``setup()`` (it runs on a worker, where this repo is
    not importable), so replacing the module in ``sys.modules`` is enough.
    """
    _FakeModel.instances = []
    _FakeModel.dim = FAKE_DIM
    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = _FakeModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    return _FakeModel


def _build_udf(**overrides):
    kwargs: dict[str, Any] = dict(manifest=None, dim=FAKE_DIM, batch_size=2)
    kwargs.update(overrides)
    return embed_mod.build_embed_description_udf(**kwargs)


# --- dimension resolution ---------------------------------------------------


def test_resolve_dim_uses_explicit_value_over_the_table():
    assert embed_mod.resolve_dim(embed_mod.DEFAULT_MODEL, 16) == 16


def test_resolve_dim_looks_up_known_checkpoints():
    assert embed_mod.resolve_dim("sentence-transformers/all-MiniLM-L6-v2") == 384
    assert embed_mod.resolve_dim("sentence-transformers/all-mpnet-base-v2") == 768


def test_resolve_dim_demands_dim_for_unknown_models():
    with pytest.raises(ValueError, match="unknown embedding width"):
        embed_mod.resolve_dim("acme/some-new-encoder")


# --- UDF binding (real geneva decorator) ------------------------------------


def test_factory_binds_text_column_to_a_fixed_size_vector():
    udf = _build_udf(dim=0)  # 0 -> inferred from the default checkpoint

    assert udf.input_columns == ["description"]
    assert udf.data_type == pa.list_(pa.float32(), 384)
    # The producing checkpoint is recorded on the column itself.
    assert udf.field_metadata == {
        "model": embed_mod.DEFAULT_MODEL,
        "normalized": "l2",
    }
    # Fresh version per build, so rebuilding the view re-materializes vectors.
    assert udf.version != _build_udf(dim=0).version


def test_factory_rebinds_input_column_model_and_resources():
    udf = _build_udf(
        input_column="title",
        model_name="sentence-transformers/all-mpnet-base-v2",
        dim=0,
        num_gpus=0,
    )
    assert udf.input_columns == ["title"]
    assert udf.data_type == pa.list_(pa.float32(), 768)
    assert udf.num_gpus == 0


# --- UDF execution (fake model) --------------------------------------------


def test_udf_embeds_every_row_in_one_batched_call(fake_st):
    udf = _build_udf()
    out = udf(pa.array(["alpha", "beta longer text"]))

    assert out.type == pa.list_(pa.float32(), FAKE_DIM)
    assert out.to_pylist() == [[5.0] * FAKE_DIM, [16.0] * FAKE_DIM]
    # One model, one encode call for the whole array — that's the batch UDF's point.
    assert len(fake_st.instances) == 1
    assert fake_st.instances[0].encoded == [["alpha", "beta longer text"]]
    # Vectors are asked for L2-normalized, so KNN ranks by cosine similarity.
    assert fake_st.instances[0].encode_kwargs["normalize_embeddings"] is True
    assert fake_st.instances[0].encode_kwargs["batch_size"] == 2


def test_udf_loads_the_model_once_across_calls(fake_st):
    udf = _build_udf()
    udf(pa.array(["one"]))
    udf(pa.array(["two"]))
    # setup() is idempotent: a second task on the same actor reuses the model.
    assert len(fake_st.instances) == 1
    assert fake_st.instances[0].encoded == [["one"], ["two"]]


def test_udf_leaves_null_and_blank_rows_null(fake_st):
    udf = _build_udf()
    out = udf(pa.array(["alpha", None, "   ", "gamma"]))

    values = out.to_pylist()
    assert values[1] is None and values[2] is None
    assert values[0] == [5.0] * FAKE_DIM and values[3] == [5.0] * FAKE_DIM
    # Only the rows with real text reached the model — no embedding of "".
    assert fake_st.instances[0].encoded == [["alpha", "gamma"]]


def test_udf_returns_all_nulls_without_calling_the_model(fake_st):
    udf = _build_udf()
    out = udf(pa.array([None, None], type=pa.string()))

    assert out.to_pylist() == [None, None]
    assert fake_st.instances[0].encoded == []  # loaded, never asked to encode


def test_udf_handles_an_empty_task(fake_st):
    udf = _build_udf()
    out = udf(pa.array([], type=pa.string()))

    assert len(out) == 0
    assert out.type == pa.list_(pa.float32(), FAKE_DIM)
    assert fake_st.instances[0].encoded == []  # nothing handed to the model


def test_udf_rejects_a_model_whose_width_differs_from_the_column(fake_st):
    fake_st.dim = FAKE_DIM * 2  # column says 4, checkpoint emits 8
    udf = _build_udf()

    with pytest.raises(ValueError, match=f"--dim {FAKE_DIM * 2}"):
        udf(pa.array(["alpha"]))


def test_udf_reads_the_width_from_the_legacy_getter(monkeypatch: pytest.MonkeyPatch):
    """Older sentence-transformers only has ``get_sentence_embedding_dimension``.

    The worker's pinned version need not match the client's spelling, so the
    dimension check accepts either name.
    """

    class _LegacyModel(_FakeModel):
        get_embedding_dimension = None  # type: ignore[assignment]

        def get_sentence_embedding_dimension(self) -> int:
            return self.dim

    _FakeModel.instances = []
    _FakeModel.dim = FAKE_DIM
    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = _LegacyModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)

    out = _build_udf()(pa.array(["alpha"]))
    assert out.to_pylist() == [[5.0] * FAKE_DIM]


def test_encode_query_returns_a_plain_float_vector(fake_st):
    vector = embed_mod.encode_query("warm jacket", model_name="acme/test")

    assert vector == [11.0] * FAKE_DIM
    assert all(isinstance(x, float) for x in vector)
    assert fake_st.instances[0].model_name == "acme/test"
    assert fake_st.instances[0].encode_kwargs["normalize_embeddings"] is True


# --- ingest CLI -------------------------------------------------------------


def test_ingest_products_creates_the_table_with_stable_row_ids(
    monkeypatch: pytest.MonkeyPatch,
):
    from geneva_examples.examples.text import ingest as mod

    conn = FakeConn(table=FakeTable(names=list(products_mod.SCHEMA.names)))
    monkeypatch.setattr(mod, "connect", lambda _cfg: conn)

    result = CliRunner().invoke(
        cli.ingest_products, ["--mode", "local", "--rows", "10", "--frag-size", "4"]
    )

    assert result.exit_code == 0, result.output
    assert "products" in conn.dropped  # overwrite=True dropped it first
    assert "products" in conn.created
    # A view can only refresh across source versions if the source has these.
    assert conn.create_kwargs["products"]["storage_options"] == {
        "new_table_enable_stable_row_ids": "true"
    }
    # 10 rows at frag_size 4 -> create_table(batch0) + 2 appends.
    assert len(conn.created["products"].adds) == 2


def test_ingest_products_appends_and_renumbers_with_no_overwrite(
    monkeypatch: pytest.MonkeyPatch,
):
    from geneva_examples.examples.text import ingest as mod

    class _Existing(FakeTable):
        def count_rows(self, _filter: str | None = None) -> int:
            return 40  # catalog already has 40 rows

    table = _Existing(names=list(products_mod.SCHEMA.names))
    conn = FakeConn(table=table)
    monkeypatch.setattr(mod, "connect", lambda _cfg: conn)

    result = CliRunner().invoke(
        cli.ingest_products,
        ["--mode", "local", "--no-overwrite", "--rows", "3", "--frag-size", "3"],
    )

    assert result.exit_code == 0, result.output
    assert conn.dropped == []  # nothing dropped
    assert "products" not in conn.created  # appended into the existing table
    ids = [r["product_id"] for r in table.adds[0].to_pylist()]
    # Renumbered from the existing row count, so ids stay unique after an append.
    assert ids == ["prod-00040", "prod-00041", "prod-00042"]


def test_ingest_products_creates_the_table_when_no_overwrite_finds_none(
    monkeypatch: pytest.MonkeyPatch,
):
    from geneva_examples.examples.text import ingest as mod

    conn = FakeConn()  # open_table raises: no table yet
    monkeypatch.setattr(mod, "connect", lambda _cfg: conn)

    result = CliRunner().invoke(
        cli.ingest_products, ["--mode", "local", "--no-overwrite", "--rows", "2"]
    )

    assert result.exit_code == 0, result.output
    assert "products" in conn.created


# --- enrich CLI (the materialized view) ------------------------------------


class _ProjectionRecordingTable(FakeTable):
    """Records the projection handed to ``select()`` and any vector search."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.projections: list[Any] = []
        self.searches: list[Any] = []

    def search(self, *args, **kwargs):
        if args and args[0] is not None:
            self.searches.append(args[0])
        return super().search(*args, **kwargs)

    def select(self, columns=None, *args, **kwargs):
        # Only the view's projection is a dict; the steps' log samples select a
        # plain list of column names, which is not what these tests are about.
        if isinstance(columns, dict):
            self.projections.append(columns)
        return super().select(columns, *args, **kwargs)


def _enrich_conn(
    monkeypatch: pytest.MonkeyPatch, table: FakeTable | None = None
) -> FakeConn:
    from geneva_examples.examples.text import enrich as mod

    table = table or _ProjectionRecordingTable(names=list(products_mod.SCHEMA.names))
    conn = FakeConn(table=table, is_remote=False)
    monkeypatch.setattr(mod, "connect", lambda _cfg: conn)
    monkeypatch.setattr(mod, "runtime_session", lambda *_a, **_k: nullcontext())
    # The search demo would otherwise load the real model on the driver.
    monkeypatch.setattr(embed_mod, "encode_query", lambda *_a, **_k: [0.5] * 384)
    return conn


def test_enrich_creates_the_view_with_the_udf_in_its_projection(
    monkeypatch: pytest.MonkeyPatch,
):
    table = _ProjectionRecordingTable(names=list(products_mod.SCHEMA.names))
    conn = _enrich_conn(monkeypatch, table)

    result = CliRunner().invoke(cli.enrich_products, ["--mode", "local"])

    assert result.exit_code == 0, result.output
    assert "products_enriched_mv" in conn.dropped  # overwrite=True
    assert "products_enriched_mv" in conn.created
    projection = table.projections[0]
    # Catalog columns projected by name, plus the embedding UDF — the whole point
    # of the example: one select() carrying both plain columns and computed ones.
    assert list(projection) == [
        "product_id",
        "title",
        "description",
        "category",
        "word_count",
        "embedding",
    ]
    for name in products_mod.SCHEMA.names:
        assert projection[name] == name
    udf = projection["embedding"]
    assert udf.input_columns == ["description"]
    assert udf.data_type == pa.list_(pa.float32(), 384)


def test_enrich_honors_renamed_columns_and_model(monkeypatch: pytest.MonkeyPatch):
    table = _ProjectionRecordingTable(names=list(products_mod.SCHEMA.names))
    _enrich_conn(monkeypatch, table)

    result = CliRunner().invoke(
        cli.enrich_products,
        [
            "--mode",
            "local",
            "--input-column",
            "title",
            "--output-column",
            "title_vec",
            "--model-name",
            "sentence-transformers/all-mpnet-base-v2",
        ],
    )

    assert result.exit_code == 0, result.output
    projection = table.projections[0]
    assert "title_vec" in projection and "embedding" not in projection
    assert projection["title_vec"].input_columns == ["title"]
    # Width follows the checkpoint without the caller restating it.
    assert projection["title_vec"].data_type == pa.list_(pa.float32(), 768)


def test_enrich_refuses_a_source_without_stable_row_ids(
    monkeypatch: pytest.MonkeyPatch,
):
    """Without stable row IDs the view dies the first time the source version
    moves — which the maintenance agent's compaction does unprompted. Fail up
    front, naming the table, instead of leaving a view that breaks later."""
    table = _ProjectionRecordingTable(
        names=list(products_mod.SCHEMA.names), stable_row_ids=False
    )
    conn = _enrich_conn(monkeypatch, table)

    result = CliRunner().invoke(cli.enrich_products, ["--mode", "local"])

    assert result.exit_code != 0
    assert "stable row IDs" in str(result.exception)
    assert "products" in str(result.exception)
    assert "products_enriched_mv" not in conn.created  # nothing half-built


def test_enrich_refreshes_the_existing_view_with_no_overwrite(
    monkeypatch: pytest.MonkeyPatch,
):
    table = _ProjectionRecordingTable(names=list(products_mod.SCHEMA.names))
    conn = _enrich_conn(monkeypatch, table)

    result = CliRunner().invoke(
        cli.enrich_products, ["--mode", "local", "--no-overwrite"]
    )

    assert result.exit_code == 0, result.output
    assert conn.dropped == []
    # Reused the existing view: no create, and no new projection was built.
    assert "products_enriched_mv" not in conn.created
    assert table.projections == []


def test_enrich_creates_the_view_when_no_overwrite_finds_none(
    monkeypatch: pytest.MonkeyPatch,
):
    from geneva_examples.examples.text import enrich as mod

    class _SourceOnlyConn(FakeConn):
        """Opens the catalog but 404s the view, as on a first --no-overwrite run."""

        def open_table(self, name: str, **kwargs: Any) -> FakeTable:
            if name == "products_enriched_mv":
                raise RuntimeError("table not found: products_enriched_mv")
            return super().open_table(name, **kwargs)

    table = _ProjectionRecordingTable(names=list(products_mod.SCHEMA.names))
    conn = _SourceOnlyConn(table=table, is_remote=False)
    monkeypatch.setattr(mod, "connect", lambda _cfg: conn)
    monkeypatch.setattr(mod, "runtime_session", lambda *_a, **_k: nullcontext())
    monkeypatch.setattr(embed_mod, "encode_query", lambda *_a, **_k: [0.5] * 384)

    result = CliRunner().invoke(
        cli.enrich_products, ["--mode", "local", "--no-overwrite"]
    )

    assert result.exit_code == 0, result.output
    assert "products_enriched_mv" in conn.created
    assert table.projections  # fell through to building the view


def test_enrich_search_demo_queries_the_view_vectors(monkeypatch: pytest.MonkeyPatch):
    table = _ProjectionRecordingTable(names=list(products_mod.SCHEMA.names))
    _enrich_conn(monkeypatch, table)

    result = CliRunner().invoke(
        cli.enrich_products, ["--mode", "local", "--query-text", "warm jacket"]
    )

    assert result.exit_code == 0, result.output
    assert table.searches == [[0.5] * 384]  # the encoded query, KNN'd on the view


def test_enrich_skips_the_search_demo_when_disabled(monkeypatch: pytest.MonkeyPatch):
    table = _ProjectionRecordingTable(names=list(products_mod.SCHEMA.names))
    _enrich_conn(monkeypatch, table)
    # A driver-side model load here would be a bug: --no-search-demo exists so
    # the step runs without torch on the driver.
    monkeypatch.setattr(
        embed_mod,
        "encode_query",
        lambda *_a, **_k: pytest.fail("encode_query called with --no-search-demo"),
    )

    result = CliRunner().invoke(
        cli.enrich_products, ["--mode", "local", "--no-search-demo"]
    )

    assert result.exit_code == 0, result.output
    assert table.searches == []
