@pytest.fixture(autouse=True)
def isolated_handler_provider():
    # Golden evidence describes offline discovery. Earlier daemon tests can
    # leave a live provider whose plugin definitions include runtime metadata.
    set_handler_provider(None)
    try:
        yield
    finally:
        set_handler_provider(None)


