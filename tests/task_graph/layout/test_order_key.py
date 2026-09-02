from src.task_graph.layout.order_key import between


def test_between_none_none_gives_middle():
    assert between(None, None) == "U"


def test_between_orders_and_is_stable():
    a = between(None, None)
    b = between(a, None)
    c = between(a, b)
    assert a < c < b


def test_many_inserts_at_front_keep_ordering():
    keys = [between(None, None)]
    for _ in range(200):
        keys.append(between(None, keys[-1]))
    assert keys == sorted(keys, reverse=True)


def test_many_inserts_between_two_keys_keep_ordering():
    lo, hi = between(None, None), between(between(None, None), None)
    keys = []
    prev = lo
    for _ in range(200):
        prev = between(prev, hi)
        keys.append(prev)
    assert keys == sorted(keys)
    assert all(lo < k < hi for k in keys)
