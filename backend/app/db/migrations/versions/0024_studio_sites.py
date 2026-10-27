"""studios: a social network or a public mailbox is no studio site, a careers subdomain is the company's site

Revision ID: 0024_studio_sites
Revises: 0023_studios
Create Date: 2026-09-24

The first version stored vk.com and instagram.com — the «site» of hh cards — as studio domains, the
dedupe key of studios, so a second employer with such a «site» merged into the first one; and careers.bitrix24.ru,
which did not open while bitrix24.ru does. New studios get the fix in services/studios.studio_site; this repairs the
stored ones: a foreign domain goes (the link stays as the contact page), a careers subdomain becomes the company's
domain and its contacts are checked again. The domain lists are a snapshot of studios.NOT_A_SITE and
PUBLIC_MAIL_DOMAINS of this revision.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import sqlalchemy as sa
from alembic import op

revision = "0024_studio_sites"
down_revision = "0023_studios"
branch_labels = None
depends_on = None

FOREIGN = (
    "t.me telegram.me telegram.org vk.com vk.ru vkvideo.ru ok.ru max.ru dzen.ru rutube.ru hh.ru headhunter.ru "
    "docs.google.com forms.gle forms.yandex.ru notion.so notion.site youtube.com youtu.be instagram.com tiktok.com "
    "pinterest.com linkedin.com habr.com github.com bit.ly clck.ru typeform.com careers-page.com lever.co ashbyhq.com "
    "greenhouse.io workable.com smartrecruiters.com bamboohr.com recruitee.com teamtailor.com personio.de huntflow.ru "
    "potok.io getmatch.ru geekjob.ru superjob.ru rabota.ru zarplata.ru trudvsem.ru avito.ru hh.kz rabota.by "
    "careerlaboratory.ru facancy.ru cryptojobslist.com cjl.ist hashtagweb3.com lnkd.in x.com twitter.com facebook.com "
    "whatsapp.com wa.me viber.com vc.ru dtf.ru tproger.ru luma.com mail.ru yandex.ru google.com forms.office.com "
    "airtable.com calendly.com zoom.us behance.net dribbble.com taplink.cc linktr.ee vk.cc t.co goo.gl tinyurl.com "
    "gmail.com googlemail.com bk.ru inbox.ru list.ru internet.ru ya.ru yandex.com rambler.ru outlook.com hotmail.com "
    "live.com icloud.com me.com yahoo.com proton.me protonmail.com"
).split()
SOCIAL = "vk.com vk.ru ok.ru t.me telegram.me instagram.com facebook.com dzen.ru youtube.com behance.net".split()
SOCIAL += "dribbble.com taplink.cc linktr.ee".split()
CAREER_SUBDOMAINS = ("careers.", "career.", "jobs.", "job.", "hr.")


def _under(domain: str, parents: list[str]) -> bool:
    return any(domain == p or domain.endswith("." + p) for p in parents)


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, domain, site_url, contact_url FROM studios WHERE domain IS NOT NULL")).all()
    taken = {str(r.domain) for r in rows}
    for row in rows:
        domain = str(row.domain)
        if _under(domain, FOREIGN):
            contact = row.contact_url or (row.site_url if _under(domain, SOCIAL) else None)
            conn.execute(
                sa.text("UPDATE studios SET domain = NULL, site_url = NULL, contact_url = :contact WHERE id = :id"),
                {"contact": contact, "id": row.id},
            )
            continue
        prefix = next((p for p in CAREER_SUBDOMAINS if domain.startswith(p)), None)
        if prefix is None or domain.count(".") < 2 or domain[len(prefix) :] in taken:
            continue  # a clash: another studio already has the company's domain — this one is left as it is
        company = domain[len(prefix) :]
        scheme = urlsplit(str(row.site_url or "")).scheme or "https"
        conn.execute(
            sa.text(
                "UPDATE studios SET domain = :domain, site_url = :site, contacts_checked_at = NULL, "
                "contacts_error = NULL, status = CASE WHEN status = 'no_contacts' THEN 'new' ELSE status END "
                "WHERE id = :id"
            ),
            {"domain": company, "site": f"{scheme}://{company}", "id": row.id},
        )
        taken.add(company)


def downgrade() -> None:
    pass  # a data repair: the foreign «sites» are not put back
