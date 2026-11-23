"""冒烟测试：验证包能导入。"""


def test_import_playranger() -> None:
    """包应该能正常 import，且有 __version__。"""
    import playranger

    assert hasattr(playranger, "__version__")
    assert isinstance(playranger.__version__, str)
    assert len(playranger.__version__) > 0
