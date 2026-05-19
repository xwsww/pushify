from functools import lru_cache

COLORS = [
    'blue',
    'indigo',
    'violet',
    'purple',
    'fuchsia',
    'pink',
    'rose',
    'red',
    'orange',
    'amber',
    'yellow',
    'lime',
    'green',
    'emerald',
    'teal',
    'cyan',
    'sky',
]

@lru_cache
def get_color(id: str | int) -> str:
    """Get a deterministic color based on an ID."""
    color_index = sum(ord(c) for c in str(id)) % len(COLORS)
    return COLORS[color_index]