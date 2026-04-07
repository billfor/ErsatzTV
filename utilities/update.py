#!/usr/bin/env python3
"""
ErsatzTV Playout Anchor Offset Utility
=======================================
Inspect and update collection enumerator state (episode index) for a playout
directly in the SQLite database, bypassing EF Core.

ErsatzTV MUST be shut down before running a set operation.

How it works:
  ErsatzTV maintains checkpoint anchors (one per built day) and a continue
  anchor (current position). Each checkpoint records where the series was at
  the end of that day's build, so checkpoints naturally have different indexes -
  this is normal and correct. Setting an offset updates ALL anchors to the
  desired index so that whichever checkpoint Refresh uses, it gets the right value.
  PlayoutAnchor.NextStart is left completely untouched.

Usage:
    ./update.py -d /path/to/ersatztv.sqlite3 -p 1 -l
    ./update.py -d /path/to/ersatztv.sqlite3 -p 1 -t show -i 842 -s 40
    ./update.py -d /path/to/ersatztv.sqlite3 -p 1 -t collection -i 1 -s 10
    ./update.py -d /path/to/ersatztv.sqlite3 -p 1 -t smart -i 2 -s 15

Options:
    -d  Path to the ErsatzTV SQLite database file (required)
    -p  Playout ID (required)
    -l  List all collections for the playout with current index and total count
    -t  Collection type: show | collection | smart  (required when using -i and -s)
    -i  ID of the item to target (MediaItemId, CollectionId, or SmartCollectionId)
    -s  Index to set (0-based)

IMPORTANT: ErsatzTV must be shut down before running -s.
After setting, restart ErsatzTV. It is recomended to refresh the playout once or twice (may not be needed)
"""

import argparse
import re
import sqlite3
import sys


TYPE_MAP = {
    "show":       {"column": "MediaItemId",       "collection_type": 1},
    "collection": {"column": "CollectionId",      "collection_type": 0},
    "smart":      {"column": "SmartCollectionId", "collection_type": 5},
}


def get_connection(db_path):
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.OperationalError as e:
        print(f"Error opening database: {e}", file=sys.stderr)
        sys.exit(1)


def get_all_collections(cur, playout_id):
    """Return distinct collection keys for a playout."""
    cur.execute("""
        SELECT
            a.CollectionType,
            a.CollectionId,
            a.MediaItemId,
            a.SmartCollectionId,
            a.MultiCollectionId,
            a.RerunCollectionId,
            a.PlaylistId,
            a.SearchQuery,
            a.FakeCollectionKey
        FROM PlayoutProgramScheduleAnchor a
        WHERE a.PlayoutId = ?
        GROUP BY a.CollectionType, a.CollectionId, a.MediaItemId,
                 a.SmartCollectionId, a.MultiCollectionId, a.RerunCollectionId,
                 a.PlaylistId, a.SearchQuery, a.FakeCollectionKey
        ORDER BY a.CollectionType, a.MediaItemId, a.CollectionId, a.SmartCollectionId
    """, (playout_id,))
    return cur.fetchall()


def get_anchors_for_collection(cur, playout_id, col):
    """Get all anchors for a specific collection key."""
    cur.execute("""
        SELECT a.Id, a.AnchorDate, e."Index", e.Seed
        FROM PlayoutProgramScheduleAnchor a
        LEFT JOIN CollectionEnumeratorState e ON e.PlayoutProgramScheduleAnchorId = a.Id
        WHERE a.PlayoutId = ?
          AND a.CollectionType = ?
          AND (a.CollectionId IS ? OR a.CollectionId = ?)
          AND (a.MediaItemId IS ? OR a.MediaItemId = ?)
          AND (a.SmartCollectionId IS ? OR a.SmartCollectionId = ?)
          AND (a.MultiCollectionId IS ? OR a.MultiCollectionId = ?)
          AND (a.RerunCollectionId IS ? OR a.RerunCollectionId = ?)
          AND (a.PlaylistId IS ? OR a.PlaylistId = ?)
          AND (a.SearchQuery IS ? OR a.SearchQuery = ?)
          AND (a.FakeCollectionKey IS ? OR a.FakeCollectionKey = ?)
        ORDER BY a.AnchorDate, a.Id
    """, (
        playout_id,
        col["CollectionType"],
        col["CollectionId"], col["CollectionId"],
        col["MediaItemId"], col["MediaItemId"],
        col["SmartCollectionId"], col["SmartCollectionId"],
        col["MultiCollectionId"], col["MultiCollectionId"],
        col["RerunCollectionId"], col["RerunCollectionId"],
        col["PlaylistId"], col["PlaylistId"],
        col["SearchQuery"], col["SearchQuery"],
        col["FakeCollectionKey"], col["FakeCollectionKey"],
    ))
    return cur.fetchall()


def current_index(anchors):
    """
    Return the current index from the continue anchor (AnchorDate IS NULL),
    or the highest checkpoint index if no continue anchor exists.
    """
    continue_anchors = sorted(
        [a for a in anchors if a["AnchorDate"] is None and a["Index"] is not None],
        key=lambda a: a["Id"], reverse=True
    )
    if continue_anchors:
        return continue_anchors[0]["Index"]

    checkpoint_anchors = [a for a in anchors
                          if a["AnchorDate"] is not None and a["Index"] is not None]
    if checkpoint_anchors:
        return max(a["Index"] for a in checkpoint_anchors)

    return 0


def get_seed(anchors):
    return next((a["Seed"] for a in anchors if a["Seed"] is not None), 12345)


def set_all_anchors(cur, anchors, index, seed):
    """Update all anchors for a collection to the given index."""
    for a in anchors:
        cur.execute("""
            INSERT INTO CollectionEnumeratorState (PlayoutProgramScheduleAnchorId, Seed, "Index")
            VALUES (?, ?, ?)
            ON CONFLICT(PlayoutProgramScheduleAnchorId)
            DO UPDATE SET "Index" = excluded."Index"
        """, (a["Id"], seed, index))


def extract_show_name_from_query(query):
    """Best-effort extraction of a show title from an ErsatzTV search query."""
    patterns = [
        r'show:title\s+"([^"*]+)',
        r"show:title\s+'([^'*]+)",
        r'show_title:\s*(.+)',
        r'show:title\s+(\w[\w\s]+)',
    ]
    for p in patterns:
        m = re.search(p, query or "", re.IGNORECASE)
        if m:
            return m.group(1).strip().rstrip("*").strip()
    return None


def get_episode_count(cur, col):
    """Return total episode count for a collection, or None if unknown."""
    ct = col["CollectionType"]
    try:
        if ct == 1 and col["MediaItemId"]:
            cur.execute("""
                SELECT COUNT(e.Id) as cnt FROM Episode e
                JOIN Season s ON s.Id = e.SeasonId
                WHERE s.ShowId = ? AND s.SeasonNumber > 0
            """, (col["MediaItemId"],))
            r = cur.fetchone()
            return r["cnt"] if r else None

        if ct == 0 and col["CollectionId"]:
            # Items in a manual collection can be Shows, Seasons, Episodes or Movies.
            # Count episodes for Shows and Seasons, count directly for Episodes/Movies.
            cur.execute("""
                SELECT ci.MediaItemId FROM CollectionItem ci WHERE ci.CollectionId = ?
            """, (col["CollectionId"],))
            item_ids = [r["MediaItemId"] for r in cur.fetchall()]
            if not item_ids:
                return 0
            placeholders = ",".join("?" * len(item_ids))
            # Count episodes belonging to shows in the collection
            cur.execute(f"""
                SELECT COUNT(e.Id) as cnt FROM Episode e
                JOIN Season s ON s.Id = e.SeasonId AND s.SeasonNumber > 0
                WHERE s.ShowId IN ({placeholders})
            """, item_ids)
            show_eps = cur.fetchone()["cnt"]
            # Count episodes belonging to seasons in the collection
            cur.execute(f"""
                SELECT COUNT(e.Id) as cnt FROM Episode e
                JOIN Season s ON s.Id = e.SeasonId AND s.SeasonNumber > 0
                WHERE e.SeasonId IN ({placeholders})
            """, item_ids)
            season_eps = cur.fetchone()["cnt"]
            # Count direct episode items
            cur.execute(f"""
                SELECT COUNT(e.Id) as cnt FROM Episode e WHERE e.Id IN ({placeholders})
            """, item_ids)
            direct_eps = cur.fetchone()["cnt"]
            # Count movies
            cur.execute(f"""
                SELECT COUNT(m.Id) as cnt FROM Movie m WHERE m.Id IN ({placeholders})
            """, item_ids)
            movies = cur.fetchone()["cnt"]
            total = show_eps + season_eps + direct_eps + movies
            return total if total > 0 else len(item_ids)

        if ct == 5 and col["SmartCollectionId"]:
            cur.execute("SELECT Query FROM SmartCollection WHERE Id = ?",
                        (col["SmartCollectionId"],))
            sc = cur.fetchone()
            if sc:
                name = extract_show_name_from_query(sc["Query"])
                if name:
                    cur.execute("""
                        SELECT COALESCE(SUM(cnt), 0) as total FROM (
                            SELECT COUNT(e.Id) as cnt
                            FROM Show s
                            JOIN ShowMetadata sm ON sm.ShowId = s.Id
                            JOIN Season sea ON sea.ShowId = s.Id AND sea.SeasonNumber > 0
                            JOIN Episode e ON e.SeasonId = sea.Id
                            WHERE LOWER(sm.Title) LIKE LOWER(?)
                            GROUP BY s.Id
                        )
                    """, (f"%{name}%",))
                    r = cur.fetchone()
                    if r and r["total"]:
                        return r["total"]
    except Exception:
        pass
    return None


def resolve_name(cur, col):
    ct = col["CollectionType"]
    if ct == 1 and col["MediaItemId"]:
        cur.execute("SELECT sm.Title FROM ShowMetadata sm WHERE sm.ShowId = ? LIMIT 1",
                    (col["MediaItemId"],))
        r = cur.fetchone()
        return r["Title"] if r else f"Show {col['MediaItemId']}"

    if ct == 0 and col["CollectionId"]:
        cur.execute("SELECT Name FROM Collection WHERE Id = ?", (col["CollectionId"],))
        r = cur.fetchone()
        return r["Name"] if r else f"Collection {col['CollectionId']}"

    if ct == 5 and col["SmartCollectionId"]:
        cur.execute("SELECT Name FROM SmartCollection WHERE Id = ?", (col["SmartCollectionId"],))
        r = cur.fetchone()
        return r["Name"] if r else f"Smart {col['SmartCollectionId']}"

    if col["SearchQuery"]:
        return f"Search: {col['SearchQuery'][:30]}"

    return f"Unknown (type={ct})"


def get_item_id(col):
    return (col["MediaItemId"] or col["CollectionId"] or
            col["SmartCollectionId"] or col["MultiCollectionId"] or "?")


def type_label(ct):
    return {0: "collection", 1: "show", 5: "smart"}.get(ct, f"type={ct}")


def cmd_list(con, playout_id):
    cur = con.cursor()
    collections = get_all_collections(cur, playout_id)

    if not collections:
        print(f"No anchors found for playout {playout_id}.")
        print("The playout may not have built yet.")
        return

    print(f"Anchors for playout {playout_id}:\n")
    print(f"  {'Type':<12} {'ItemId':<8} {'Name':<30} {'Index':<7} {'Total':<7} {'Anchors'}")
    print(f"  {'-'*10:<12} {'-'*6:<8} {'-'*28:<30} {'-'*5:<7} {'-'*5:<7} {'-'*7}")

    for col in collections:
        anchors = get_anchors_for_collection(cur, playout_id, col)
        name = resolve_name(cur, col)
        tl = type_label(col["CollectionType"])
        item_id = get_item_id(col)
        idx = current_index(anchors)
        count = get_episode_count(cur, col)
        count_str = str(count) if count is not None else "?"
        print(f"  {tl:<12} {str(item_id):<8} {name:<30} {str(idx):<7} {count_str:<7} {len(anchors)}")

    print()
    cur.execute("SELECT NextStart FROM PlayoutAnchor WHERE PlayoutId = ?", (playout_id,))
    pa = cur.fetchone()
    print(f"  PlayoutAnchor.NextStart : {pa['NextStart'] if pa else 'none'}")


def cmd_set(con, playout_id, type_key, item_id, index):
    type_info = TYPE_MAP[type_key]
    column = type_info["column"]
    collection_type = type_info["collection_type"]

    cur = con.cursor()

    cur.execute(f"""
        SELECT a.Id, a.AnchorDate, e."Index", e.Seed
        FROM PlayoutProgramScheduleAnchor a
        LEFT JOIN CollectionEnumeratorState e ON e.PlayoutProgramScheduleAnchorId = a.Id
        WHERE a.PlayoutId = ?
          AND a.CollectionType = ?
          AND a.{column} = ?
        ORDER BY a.AnchorDate, a.Id
    """, (playout_id, collection_type, item_id))

    all_anchors = cur.fetchall()

    if not all_anchors:
        print(f"No anchors found for playout {playout_id}, type '{type_key}', id {item_id}.",
              file=sys.stderr)
        print("Tip: run -l to list available items.", file=sys.stderr)
        sys.exit(1)

    seed = get_seed(all_anchors)
    cur_idx = current_index(all_anchors)

    print(f"Found {len(all_anchors)} anchor(s)")
    print(f"Current index : {cur_idx}")
    print(f"Setting index : {index}")

    # Update ALL anchors (continue and checkpoints) to the desired index.
    # Checkpoints naturally have different values (daily progression) but
    # all need to be set to the same value so Refresh picks up the right
    # position regardless of which checkpoint date matches today's window.
    # PlayoutAnchor.NextStart is left completely untouched.
    set_all_anchors(cur, all_anchors, index, seed)

    for a in all_anchors:
        label = "continue" if a["AnchorDate"] is None else f"checkpoint {a['AnchorDate']}"
        print(f"  Updated anchor {a['Id']} ({label}) -> index {index}")

    # Find the oldest checkpoint date to use as the new NextStart.
    # This causes ErsatzTV on startup to do a Continue build from that date,
    # picking up our new anchor values and rebuilding all items correctly.
    cur.execute("""
        SELECT MIN(AnchorDate) as oldest FROM PlayoutProgramScheduleAnchor
        WHERE PlayoutId = ? AND AnchorDate IS NOT NULL
    """, (playout_id,))
    oldest = cur.fetchone()["oldest"]

    if oldest:
        # Set NextStart to now (UTC) so the Continue build on startup starts
        # from the current time. Using the oldest checkpoint date (past) would
        # schedule the first episode in the past, making the user see episode+1
        # as "next". Starting from now ensures the first episode is upcoming.
        from datetime import datetime, timezone
        now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        cur.execute("""
            UPDATE PlayoutAnchor SET NextStart = ? WHERE PlayoutId = ?
        """, (now_utc, playout_id))
        print(f"  Set PlayoutAnchor.NextStart to {now_utc} (now UTC)")

        # Clear playout items so the Continue build rebuilds them correctly
        # from the oldest checkpoint using the new anchor values
        cur.execute("""DELETE FROM PlayoutItemGraphicsElement
                       WHERE PlayoutItemId IN (SELECT Id FROM PlayoutItem WHERE PlayoutId = ?)""",
                    (playout_id,))
        cur.execute("""DELETE FROM PlayoutItemWatermark
                       WHERE PlayoutItemId IN (SELECT Id FROM PlayoutItem WHERE PlayoutId = ?)""",
                    (playout_id,))
        cur.execute("DELETE FROM PlayoutItem WHERE PlayoutId = ?", (playout_id,))
        cur.execute("DELETE FROM PlayoutGap WHERE PlayoutId = ?", (playout_id,))
        print(f"  Cleared existing playout items")

    con.commit()
    print(f"\nDone. Restart ErsatzTV — it will rebuild from index {index}.")


def main():
    parser = argparse.ArgumentParser(
        description="ErsatzTV playout anchor offset utility",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  List all collections for playout 1:
    ./update.py -d ersatztv.sqlite3 -p 1 -l

  Set Danger Man (show id 842) to index 40:
    ./update.py -d ersatztv.sqlite3 -p 1 -t show -i 842 -s 40

  Set Flintstones (manual collection id 1) to index 10:
    ./update.py -d ersatztv.sqlite3 -p 1 -t collection -i 1 -s 10

  Set Get Smart (smart collection id 2) to index 15:
    ./update.py -d ersatztv.sqlite3 -p 1 -t smart -i 2 -s 15

IMPORTANT: ErsatzTV must be shut down before running -s.
After setting, restart ErsatzTV. No UI Refresh needed.
        """
    )

    parser.add_argument("-d", "--db",       required=True,           help="Path to ersatztv SQLite database file")
    parser.add_argument("-p", "--playout",  required=True, type=int, help="Playout ID")
    parser.add_argument("-l", "--list",     action="store_true",     help="List collections and indexes")
    parser.add_argument("-t", "--type",     choices=["show", "collection", "smart"],
                                                                     help="Collection type (required with -i/-s)")
    parser.add_argument("-i", "--itemid",   type=int,                help="Item ID")
    parser.add_argument("-s", "--setindex", type=int,                help="Index to set (0-based)")

    args = parser.parse_args()

    if not args.list and not (args.itemid is not None and args.setindex is not None):
        parser.error("Specify -l to list, or -i and -s to set an index.")

    if (args.itemid is not None or args.setindex is not None) and not args.type:
        parser.error("-t/--type is required when using -i and -s.")

    if args.setindex is not None and args.setindex < 0:
        parser.error("Index must be 0 or greater.")

    con = get_connection(args.db)

    try:
        if args.list:
            cmd_list(con, args.playout)
        else:
            cmd_set(con, args.playout, args.type, args.itemid, args.setindex)
    finally:
        con.close()


if __name__ == "__main__":
    main()
