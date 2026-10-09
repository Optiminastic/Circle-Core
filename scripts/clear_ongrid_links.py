"""Detach BGV records from an OnGrid community they no longer belong to.

An individual id means something only inside the community it was created in.
After the move from staging to production, every id Circle stored points into
staging: the status panel either gets an error or, if that number exists in the
new community, somebody else's verification. Clearing them puts those
candidates back to "not onboarded" so BGV can be run again for real.

Nothing is deleted at OnGrid's end and no candidate data is touched - only the
fields that link a BGV record to an individual.

Dry run (default), from the curcle-be dir:

    .\\.venv\\Scripts\\python.exe scripts\\clear_ongrid_links.py

Apply it:

    .\\.venv\\Scripts\\python.exe scripts\\clear_ongrid_links.py --apply

By default it clears every record whose stored community is not the configured
one, which includes the records written before the community was stamped at
all. Restrict it to one community with --community 79355.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Running a file inside scripts/ puts scripts/ on the path, not the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import get_settings  # noqa: E402

#: Everything written by an onboard, a verify or a status read. Listed rather
#: than prefix-matched so adding an `ongrid*` field is a deliberate decision
#: about whether it should be cleared here too.
ONGRID_FIELDS = (
    "ongridIndividualId",
    "ongridCommunityId",
    "ongridBaseUrl",
    "ongridOnboardedAt",
    "ongridResponse",
    "ongridDocuments",
    "ongridDocumentIds",
    "ongridDocumentIdsFor",
    "ongridChecks",
    "ongridVerificationsSentAt",
    "ongridStatus",
    "ongridStatusAt",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument(
        "--community",
        help="only clear records stamped with this community id; default is every record not stamped with the configured one",
    )
    args = parser.parse_args()

    settings = get_settings()
    if not settings.has_database:
        raise SystemExit("DATABASE_URL is not set.")
    keep = str(settings.ongrid_community_id or "")
    if not keep and not args.community:
        raise SystemExit(
            "ONGRID_COMMUNITY_ID is not set, so there is no community to keep. "
            "Pass --community to name the one to clear instead."
        )

    engine = create_engine(settings.sqlalchemy_url)
    cleared = 0
    skipped = 0
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT id, data FROM bgvs")).fetchall()
        for row_id, data in rows:
            record: dict[str, Any] = data if isinstance(data, dict) else json.loads(data or "{}")
            individual = str(record.get("ongridIndividualId") or "")
            if not individual:
                continue

            community = str(record.get("ongridCommunityId") or "")
            target = args.community
            stale = community == target if target else community != keep
            if not stale:
                skipped += 1
                continue

            name = record.get("candidateName") or row_id
            where = community or "unstamped (pre-dates the community field)"
            print(f"  {name}: individual {individual} from {where}")

            if args.apply:
                for field in ONGRID_FIELDS:
                    record.pop(field, None)
                timeline = list(record.get("verificationTimeline") or [])
                timeline.append(
                    {
                        "date": _now(),
                        "action": (
                            f"Unlinked from OnGrid individual {individual} "
                            f"({where}) — run BGV again to create one here"
                        ),
                        "performedBy": "System",
                    }
                )
                record["verificationTimeline"] = timeline
                conn.execute(
                    text("UPDATE bgvs SET data = CAST(:d AS jsonb), updated_at = now() WHERE id = :i"),
                    {"d": json.dumps(record), "i": row_id},
                )
            cleared += 1

    engine.dispose()
    verb = "cleared" if args.apply else "would clear"
    print(f"\n{verb} {cleared} record(s); left {skipped} belonging to community {keep}.")
    if cleared and not args.apply:
        print("Re-run with --apply to write it.")


if __name__ == "__main__":
    main()
