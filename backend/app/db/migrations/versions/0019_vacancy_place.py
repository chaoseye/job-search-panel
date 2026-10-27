"""workplace of a vacancy: street address and metro stations (from hh `address`)

Revision ID: 0019_vacancy_place
Revises: 0018_search_watch_only
Create Date: 2026-09-18

"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0019_vacancy_place"
down_revision = "0018_search_watch_only"
branch_labels = None
depends_on = None


def _place(raw: str | None) -> tuple[str | None, list[str]]:
    """Same reading of hh `address` as app.hh.vacancy._address, kept local so the migration never depends on
    the application code of a later version."""
    try:
        data = json.loads(raw or "")
    except ValueError:
        return None, []
    address = data.get("address") if isinstance(data, dict) else None
    if not isinstance(address, dict):
        return None, []
    street = ", ".join(str(address.get(k)).strip() for k in ("street", "building") if address.get(k))
    if not street:
        area = data.get("area") if isinstance(data.get("area"), dict) else {}
        text = str(address.get("raw") or "").strip()
        city = str(address.get("city") or area.get("name") or "").strip()
        if city and text.lower().startswith(city.lower()):
            text = text[len(city) :].lstrip(" ,")
        street = text
    stations: list[str] = []
    for m in [address.get("metro"), *(address.get("metro_stations") or [])]:
        name = str(m.get("station_name") or "").strip() if isinstance(m, dict) else ""
        if name and name not in stations:
            stations.append(name)
    return street or None, stations


def upgrade() -> None:
    with op.batch_alter_table("vacancies") as batch:
        batch.add_column(sa.Column("address", sa.String(300), nullable=True))
        batch.add_column(sa.Column("metro_json", sa.Text(), nullable=True))
    # backfill from the stored hh JSON (the full card first, then the search item)
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, raw_short_json, raw_full_json FROM vacancies")).fetchall()
    for vid, short, full in rows:
        address, stations = _place(full)
        if not address and not stations:
            address, stations = _place(short)
        if address or stations:
            conn.execute(
                sa.text("UPDATE vacancies SET address = :a, metro_json = :m WHERE id = :id"),
                {"a": address, "m": json.dumps(stations, ensure_ascii=False), "id": vid},
            )


def downgrade() -> None:
    with op.batch_alter_table("vacancies") as batch:
        batch.drop_column("metro_json")
        batch.drop_column("address")
