"""Model sync must preserve table identity when inserting the first constraints."""

import ast

from scripts import rebuild_model_table_args as rebuild


async def test_first_sync_preserves_tablename_columns_and_is_idempotent(tmp_path, monkeypatch):
    model = tmp_path / "new_table.py"
    model.write_text(
        "class NewTable(Base):\n"
        '    __tablename__ = "new_table"\n\n'
        "    id: Mapped[str] = mapped_column(String(40), primary_key=True)\n"
        "    organization_id: Mapped[str] = mapped_column(String(40))\n"
    )

    async def read():
        return {
            "new_table": {
                "pk": ["id"],
                "fk": [],
                "index": [],
                "check": [],
                "unique": [("uq_new_table_org_id", ["organization_id", "id"])],
            }
        }

    monkeypatch.setattr(rebuild, "MODELS", tmp_path)
    monkeypatch.setattr(rebuild, "_read", read)
    assert await rebuild.main() == 0
    first = model.read_text()
    tree = ast.parse(first)
    assert any(
        isinstance(node, ast.Assign)
        and any(getattr(t, "id", None) == "__tablename__" for t in node.targets)
        for node in tree.body[0].body
    )
    assert "id: Mapped[str]" in first and "organization_id: Mapped[str]" in first
    assert await rebuild.main() == 0
    assert model.read_text() == first
