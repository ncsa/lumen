"""accepts_audio_input: strict check for the "audio" input marker."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from lumen.models.model_config import accepts_audio_input


@pytest.mark.parametrize("config, expected", [
    pytest.param(SimpleNamespace(input_modalities=None), False, id="unset"),
    pytest.param(SimpleNamespace(), False, id="no-attribute"),
    pytest.param(SimpleNamespace(input_modalities=[]), False, id="empty"),
    pytest.param(SimpleNamespace(input_modalities=["text"]), False, id="text"),
    pytest.param(SimpleNamespace(input_modalities=["text"], audio_cost_per_hour=Decimal("0.36")),
                 False, id="text-with-audio-rate"),
    pytest.param(SimpleNamespace(input_modalities="audio"), False, id="scalar-string"),
    pytest.param(SimpleNamespace(input_modalities=["audio"]), True, id="audio"),
    pytest.param(SimpleNamespace(input_modalities=["text", "audio"]), True, id="text-and-audio"),
    pytest.param(SimpleNamespace(input_modalities=("text", "audio")), True, id="snapshot-tuple"),
])
def test_accepts_audio_input(config, expected):
    assert accepts_audio_input(config) is expected
