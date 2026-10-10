import pytest

from glimt.model import Cap, Device, parse_color, to_hex, validate


def lamp() -> Device:
    return Device(id="lamp", name="Desk lamp", kind="light", bridge="b", actions=["party"], capabilities={
        "power": Cap.bool(), "brightness": Cap.percent(), "color": Cap.color(),
        "fan": Cap.enum(["low", "high"])})


def test_validate_normalises_to_the_common_vocabulary():
    clean = validate(lamp(), {"power": "on", "brightness": "49.6", "color": "Red", "fan": "low"})
    assert clean == {"power": True, "brightness": 50, "color": "#ff453a", "fan": "low"}


@pytest.mark.parametrize("changes, message", [
    ({}, "at least one setting"),
    ({"volume": 3}, "has no 'volume' setting. It can set: power, brightness, color, fan"),
    ({"brightness": 101}, "between 0 and 100"),
    ({"brightness": True}, "must be a number"),
    ({"power": "maybe"}, "true or false"),
    ({"fan": "medium"}, "one of: low, high"),
    ({"color": "#12"}, "Unrecognised colour"),
])
def test_validate_rejects_bad_input_with_a_message_a_person_can_act_on(changes, message):
    with pytest.raises(ValueError, match=message):
        validate(lamp(), changes)


def test_colours_parse_from_every_supported_form():
    assert parse_color("#0A84FF") == parse_color("0a84ff") == parse_color("blue") == (10, 132, 255)
    assert to_hex(parse_color([255, 0, 16])) == "#ff0010"
    with pytest.raises(ValueError):
        parse_color([256, 0, 0])


def test_summary_is_one_line_an_agent_can_act_on():
    assert lamp().summary() == ('lamp (light, "Desk lamp"): sets power, brightness 0-100, color, fan[low|high]; '
                                "actions party")
