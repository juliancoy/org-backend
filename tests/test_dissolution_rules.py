import importlib.util
from pathlib import Path
import sys

import pytest
from fastapi import HTTPException


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_SPEC = importlib.util.spec_from_file_location("org_backend", Path(__file__).resolve().parents[1] / "org.py")
assert _SPEC and _SPEC.loader
backend = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backend)


def test_dissolution_required_yea_rounds_up_to_three_fourths():
    assert backend._dissolution_required_yea(0) == 0
    assert backend._dissolution_required_yea(1) == 1
    assert backend._dissolution_required_yea(2) == 2
    assert backend._dissolution_required_yea(3) == 3
    assert backend._dissolution_required_yea(4) == 3
    assert backend._dissolution_required_yea(5) == 4
    assert backend._dissolution_required_yea(8) == 6


def test_dissolution_vote_result_uses_three_fourths_participating_rule():
    motion = type(
        "Motion",
        (),
        {
            "type": backend.GovernanceMotionType.DISSOLUTION.value,
            "quorum_required": 4,
            "votes": [
                type("Vote", (), {"choice": backend.GovernanceVoteChoice.YEA.value})(),
                type("Vote", (), {"choice": backend.GovernanceVoteChoice.YEA.value})(),
                type("Vote", (), {"choice": backend.GovernanceVoteChoice.YEA.value})(),
                type("Vote", (), {"choice": backend.GovernanceVoteChoice.NAY.value})(),
            ],
        },
    )()
    result = backend._governance_vote_result(motion)
    assert result["quorum_met"] is True
    assert result["participating_voters"] == 4
    assert result["required_yea"] == 3
    assert result["passed"] is True


def test_dissolution_payload_requires_asset_disposition_fields():
    payload = backend.GovernanceMotionCreate(
        type=backend.GovernanceMotionType.DISSOLUTION.value,
        title="Dissolve organization",
        body="Proposed dissolution vote",
        proposer_type=backend.GovernanceProposerType.USER.value,
        quorum_required=5,
    )
    with pytest.raises(HTTPException):
        backend._validate_dissolution_payload(payload)
