"""A generation counter of the data behind cached read models.

The analytics base (`services/analytics.cached_base`) takes seconds to build on a large base and the «Аналитика»
page asks nine reports at once, so it is built once and shared — but it must never outlive a change of the data it
reads. Every committed write to one of TRACKED tables (an ORM flush or a bulk `update()`/`delete()`/`insert()`
executed through the session) bumps the generation; a cached value built at an older generation is rebuilt.

The panel is one process and all its writes go through SQLAlchemy sessions, so the counter sees them all. A CLI
command in another process does not bump it: its writes show up after the cache's maximum age.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import ORMExecuteState, Session

# what the analytics read: vacancies and their evaluations, applications and negotiations, searches and criteria
# sets, hidden employers, the questionnaire (settings) and the resumes
TRACKED = frozenset(
    {
        "vacancies",
        "vacancy_evaluations",
        "applications",
        "negotiations",
        "saved_searches",
        "selection_profiles",
        "hidden_employers",
        "settings",
        "resumes",
    }
)
_DIRTY = "tracked_write"
_generation = 0


def generation() -> int:
    return _generation


def bump() -> None:
    global _generation
    _generation += 1


@event.listens_for(Session, "after_flush")
def _after_flush(session: Session, _flush_context: Any) -> None:
    # still the pre-flush view here: new / dirty / deleted list what the flush just wrote
    for obj in (*session.new, *session.dirty, *session.deleted):
        if getattr(obj, "__tablename__", None) in TRACKED:
            session.info[_DIRTY] = True
            return


@event.listens_for(Session, "do_orm_execute")
def _on_execute(state: ORMExecuteState) -> None:
    if state.is_update or state.is_delete or state.is_insert:
        table = getattr(state.statement, "table", None)
        if getattr(table, "name", None) in TRACKED:
            state.session.info[_DIRTY] = True


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    if session.info.pop(_DIRTY, False):
        bump()


@event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    session.info.pop(_DIRTY, None)
