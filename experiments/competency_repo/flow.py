def leaf(value):
    return value + 1


def middle(value):
    return leaf(value)


def entry(value):
    return middle(value)


def callbacks():
    return [leaf]


def dispatch(handler, value):
    return handler(value)
