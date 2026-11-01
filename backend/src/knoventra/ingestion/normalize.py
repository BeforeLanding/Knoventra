from functools import lru_cache


@lru_cache(maxsize=1)
def _t2s():
    from opencc import OpenCC

    return OpenCC("t2s")


def to_simplified(text: str) -> str:
    return _t2s().convert(text)
