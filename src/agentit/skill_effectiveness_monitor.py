"""Monitor skill approval rates and emit warnings when below threshold."""


def check_skill_effectiveness(
    skill_stats: list,
    threshold: float = 0.1,
) -> list:
    """Return warning dicts for skills whose weighted_rate < threshold."""
    warnings = []
    for stat in skill_stats:
        rate = stat.get("weighted_rate", 1.0)
        if rate < threshold:
            warnings.append({
                "skill": stat.get("skill"),
                "weighted_rate": rate,
                "action": f"Review or retrain skill '{stat.get('skill')}'; approval rate {rate:.2%} is below threshold {threshold:.2%}.",
            })
    return warnings
