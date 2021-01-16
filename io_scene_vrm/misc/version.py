from typing import Tuple


# To avoid circular reference
def version() -> Tuple[int, int, int]:
    return __import__(".".join(__name__.split(".")[:-3])).bl_info["version"]
