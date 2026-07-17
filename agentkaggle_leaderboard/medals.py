from __future__ import annotations


def medal_cutoffs(team_count: int) -> dict[str, int]:
    """Return rank-only medal-zone cutoffs from Kaggle's published progression table.

    These cutoffs do not prove that a competition or team is medal eligible. They
    are used only to label a leaderboard position as a candidate medal zone.
    """
    if team_count < 1:
        raise ValueError("team_count must be positive")
    if team_count < 100:
        return {
            "gold": int(team_count * 0.10),
            "silver": int(team_count * 0.20),
            "bronze": int(team_count * 0.40),
        }
    if team_count < 250:
        return {
            "gold": 10,
            "silver": int(team_count * 0.20),
            "bronze": int(team_count * 0.40),
        }
    if team_count < 1000:
        return {
            "gold": 10 + int(team_count * 0.002),
            "silver": 50,
            "bronze": 100,
        }
    return {
        "gold": 10 + int(team_count * 0.002),
        "silver": int(team_count * 0.05),
        "bronze": int(team_count * 0.10),
    }


def medal_candidate(rank: int, team_count: int) -> str:
    cutoffs = medal_cutoffs(team_count)
    if rank <= cutoffs["gold"]:
        return "gold"
    if rank <= cutoffs["silver"]:
        return "silver"
    if rank <= cutoffs["bronze"]:
        return "bronze"
    return "none"
