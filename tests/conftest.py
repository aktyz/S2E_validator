from __future__ import annotations

import pytest

from s2e_validator.prefilters import ExistingImage, Interaction

from helpers import make_interaction


@pytest.fixture
def interaction() -> Interaction:
    return make_interaction()


@pytest.fixture
def existing_image_interaction() -> Interaction:
    return make_interaction(image=ExistingImage("img-42"))
