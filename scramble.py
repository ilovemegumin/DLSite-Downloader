
from __future__ import annotations

import math
from pathlib import Path
from random import Random
from typing import Any


class _MTRandom(Random):

    N = 624

    def seed(self, a: int = 0, **kwargs: Any) -> None:
        mt = [a & 0xFFFFFFFF] * self.N
        for i in range(1, self.N):
            mt[i] = 1812433253 * (mt[i - 1] ^ (mt[i - 1] >> 30)) + i
            mt[i] &= 0xFFFFFFFF
        state = tuple(mt) + (self.N,)
        self.setstate((self.VERSION, state, None))


def _mt_tiles(seed: int, length: int) -> list[int]:
    if length > 624:
        raise ValueError("length は 624 以下である必要があります")
    rs = _MTRandom(seed)
    a = list(range(length))
    pos = 0
    for n in range(length - 1, -1, -1):
        e = math.floor(rs.random() * (n + 1))
        r = a[n]
        a[n] = a[e]
        a[e] = r

        pos += 1
        version, state, next_gauss = rs.getstate()
        state = state[:-1] + (pos,)
        rs.setstate((version, state, next_gauss))
    return a


def descramble(path: Path, optimized_name: str, width: int, height: int) -> None:
    from PIL import Image

    tile_w = 128
    seed = int(optimized_name[5:12], 16)

    with Image.open(path) as im:
        tiles_w = math.ceil(width / tile_w)
        tiles_h = math.ceil(height / tile_w)
        tiles = [
            im.crop(
                (
                    x * tile_w,
                    y * tile_w,
                    (x + 1) * tile_w,
                    (y + 1) * tile_w,
                )
            )
            for y in range(tiles_h)
            for x in range(tiles_w)
        ]
        new_im = im.copy()

    shuffle: dict[int, int] = {}
    for v, k in enumerate(_mt_tiles(seed, len(tiles))):
        shuffle[k] = v
    for i in range(len(tiles)):
        tile = tiles[shuffle[i]]
        x = i % tiles_w
        y = i // tiles_w
        new_im.paste(tile, (x * tile_w, y * tile_w))

    new_im = new_im.crop((0, 0, width, height))

    save_kwargs: dict[str, Any] = {}
    if path.suffix.lower() in (".jpg", ".jpeg"):
        save_kwargs["quality"] = "keep" if new_im.format == "JPEG" else 95
    new_im.save(path, **save_kwargs)
