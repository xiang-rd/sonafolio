"""Monotone CTC Viterbi alignment with optional leading/trailing audio."""

import numpy as np


def align_tokens(log_probabilities: np.ndarray, tokens: list[int], blank: int) -> list[dict] | None:
    if not tokens or len(log_probabilities) < len(tokens):
        return None
    labels = np.full(2 * len(tokens) + 1, blank, dtype=int)
    labels[1::2] = tokens
    states = len(labels)
    previous = np.full(states, -np.inf, dtype=np.float32)
    previous[0] = 0
    back = np.zeros((len(log_probabilities), states), dtype=np.uint8)
    can_skip = np.zeros(states, dtype=bool)
    can_skip[2:] = (labels[2:] != blank) & (labels[2:] != labels[:-2])
    best_score, best_frame, best_state = -np.inf, -1, -1
    for frame, probabilities in enumerate(log_probabilities):
        advance = np.concatenate(([-np.inf], previous[:-1]))
        skip = np.concatenate(([-np.inf, -np.inf], previous[:-2]))
        skip[~can_skip] = -np.inf
        candidates = np.stack((previous, advance, skip))
        choices = candidates.argmax(axis=0)
        current = candidates[choices, np.arange(states)] + probabilities[labels]
        current[0] = 0  # permit a transcript to begin after leading silence
        back[frame] = choices
        terminal = states - 2 + int(current[-1] > current[-2])
        if current[terminal] > best_score:
            best_score, best_frame, best_state = current[terminal], frame, terminal
        previous = current
    if not np.isfinite(best_score):
        return None
    frames = [[] for _ in tokens]
    frame, state = best_frame, best_state
    while frame >= 0 and state > 0:
        if state % 2:
            frames[state // 2].append(frame)
        state -= int(back[frame, state])
        frame -= 1
    if state > 0 or any(not positions for positions in frames):
        return None
    return [
        {"first_frame": min(positions), "last_frame": max(positions) + 1,
         "score": float(np.exp(log_probabilities[positions, token]).mean())}
        for token, positions in zip(tokens, frames)
    ]
