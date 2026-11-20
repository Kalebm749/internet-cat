"""The content pack loader (docs/rewrite-contracts.md §1, §4): the real pack loads, a new location needs
no code, and broken content is reported with file and line, all problems at once."""
import pytest

from cat import content
from cat.content import ContentError
from tests.rewrite_helpers import copy_pack


@pytest.fixture
def pack(tmp_path):
    return copy_pack(tmp_path)


def problems(pack) -> list[str]:
    with pytest.raises(ContentError) as e:
        content.load(pack.root)
    return e.value.problems


def has(probs: list[str], where: str, text: str) -> bool:
    return any(p.startswith(where) and text in p for p in probs)


# ---------------------------------------------------------------- the real pack

def test_real_pack_loads():
    c = content.load()
    assert set(c.locations) == {"fountain", "market"}
    assert c.character["name"] == "Nit"
    assert c.character["core"]["senses"] == ["sight", "smell", "touch"]
    assert {a.builtin for a in c.actions.values() if a.builtin} == {"move", "inspect", "sleep"}
    assert set(c.facts) == {"world.weather", "fountain.wishes", "market.offerings", "market.mailbox",
                            "market.answered"}


def test_actions_at_lists_local_then_universal():
    c = content.load()
    ids = [a.id for a in c.actions_at("fountain")]
    assert ids[:3] == ["sit_and_reflect", "make_wish", "assess_wishes"]
    assert ids[3:] == ["move", "inspect", "sleep"]


def test_spec_examples_are_in_the_pack():
    """The examples written in the spec (§4.1, §4.2, §4.8) exist with their contract."""
    c = content.load()
    wish = c.actions["make_wish"]
    assert wish.discovery == "hidden" and wish.inspect_hint and wish.inputs["wish"]["max_chars"] == 200
    assert c.actions["assess_wishes"].revisit_key == ["fountain.wishes"]
    assert c.actions["sit_and_reflect"].continuous
    assert c.facts["market.offerings"].regrow == {"amount": 1, "at_hours": [6, 12, 18]}
    assert c.actions["read_messages"].capability == "read_message"
    assert c.actions["reply_message"].discovery == "hidden"
    assert not c.capability_enabled("web_search") and c.capability_enabled("read_message")


def test_a_new_location_needs_no_code(pack):
    pack.write("locations/garden.yaml", """\
id: garden
name: the garden
description: "A small walled garden."
exits: [fountain]
facts:
  apples: {type: int, default: 1, min: 0, max: 2, regrow: {amount: 1, every_hours: 9}}
actions:
  - id: eat_apple
    name: eat an apple
    description: "Pick up a fallen apple and eat it."
    family: eat
    discovery: known_on_arrival
    requires: [{fact_gte: [garden.apples, 1]}]
    duration: {minutes: [5, 10]}
    on_finish:
      world: [{op: add, fact: garden.apples, value: -1}]
      creature: {hunger: -0.3}
""")
    pack.replace("locations/fountain.yaml", "exits: [market]", "exits: [market, garden]")
    c = content.load(pack.root)
    assert [a.id for a in c.actions_at("garden")][0] == "eat_apple"
    assert "garden" in c.locations["fountain"].exits


# ---------------------------------------------------------------- broken content

def test_all_problems_are_reported_at_once_with_file_and_line(pack):
    pack.replace("locations/market.yaml", "    family: eat\n", "    family: eat\n    colour: red\n")
    pack.replace("locations/fountain.yaml", "exits: [market]", "exits: [market, nowhere]")
    probs = problems(pack)
    assert has(probs, f"locations/market.yaml:{pack.line_of('locations/market.yaml', 'colour: red')}:",
               "unknown field 'colour'")
    assert has(probs, f"locations/fountain.yaml:{pack.line_of('locations/fountain.yaml', 'exits:')}:",
               "exit to unknown location 'nowhere'")


@pytest.mark.parametrize("rel, old, new, where_needle, text", [
    # conditions
    ("locations/fountain.yaml", "- {energy_gte: 0.1}", "- {energy_above: 0.1}",
     "energy_above", "unknown condition 'energy_above'"),
    ("locations/fountain.yaml", "- {energy_gte: 0.1}", "- {energy_gte: 3}",
     "energy_gte: 3", "between 0.0 and 1.0"),
    ("locations/fountain.yaml", "- {energy_gte: 0.1}", "- {random_lt: 0.5}",
     "random_lt: 0.5", "only allowed in an outcome"),
    ("locations/market.yaml", "{fact_gte: [market.offerings, 1]}", "{fact_gte: [market.bread, 1]}",
     "market.bread", "no fact 'market.bread'"),
    ("locations/market.yaml", "{fact_gte: [market.offerings, 1]}", "{fact_gte: [market.mailbox, 1]}",
     "fact_gte: [market.mailbox", "is a list, needs int or float"),
    # world ops
    ("locations/market.yaml", "{op: add, fact: market.offerings, value: -1}", "{op: add, fact: market.loaves, value: -1}",
     "market.loaves", "no fact 'market.loaves'"),
    ("locations/market.yaml", "{op: add, fact: market.offerings, value: -1}", "{op: explode, fact: market.offerings}",
     "explode", "unknown op 'explode'"),
    ("locations/market.yaml", "{op: add, fact: market.offerings, value: -1}", "{op: append, fact: market.offerings, value: 1}",
     "op: append, fact: market.offerings", "is a int, needs list"),
    # placeholders
    ("locations/fountain.yaml", 'text: "{inputs.wish}"', 'text: "{inputs.desire}"',
     "inputs.desire", "declares no input 'desire'"),
    ("locations/market.yaml", 'text: "I wrote a reply to a message: {inputs.reply}"',
     'text: "I replied to {capability.sender}"', "I replied to", "uses no capability"),
    ("locations/fountain.yaml", 'made: "{now}"', 'made: "{later}"', "later", "unknown placeholder {later}"),
    # actions
    ("locations/fountain.yaml", "    discovery: hidden\n    inspect_hint:", "    discovery: secret\n    inspect_hint:",
     "discovery: secret", "discovery must be one of"),
    ("locations/fountain.yaml", "duration: {minutes: [2, 5]}", "duration: {minutes: [5, 2]}",
     "duration: {minutes: [5, 2]}", "0 ≤ min ≤ max"),
    ("locations/market.yaml", "capability: read_message", "capability: telepathy",
     "- id: read_messages", "unknown capability 'telepathy'"),
    ("locations/fountain.yaml", "discover: [assess_wishes]", "discover: [sit_and_reflect]",
     "discover: [sit_and_reflect]", "which is not hidden"),
    ("locations/fountain.yaml", "creature: {mood: 0.08}", "creature: {joy: 0.08}",
     "joy: 0.08", "unknown creature variable 'joy'"),
    # facts
    ("locations/market.yaml", "offerings: {type: int, default: 2,", "offerings: {type: int, default: many,",
     "offerings:", "does not fit type int"),
    ("locations/market.yaml", "offerings: {type: int, default: 2,", "offerings: {type: number, default: 2,",
     "offerings:", "type must be one of"),
    # events and senses
    ("locations/fountain.yaml", "sense: touch,", "sense: echolocation,", "id: spray", "unknown sense 'echolocation'"),
    ("character.yaml", "senses: [sight, smell, touch]", "senses: [sight, smell, touch, radar]",
     "senses: [sight", "unknown sense 'radar'"),
    # character
    ("character.yaml", "start_location: fountain", "start_location: moon", "start_location", "unknown start_location 'moon'"),
    ("character.yaml", "innate_knowledge: [move, inspect, sleep]", "innate_knowledge: [move, inspect, sleep, fly]",
     "innate_knowledge", "innate action 'fly' does not exist"),
])
def test_broken_content_is_reported(pack, rel, old, new, where_needle, text):
    pack.replace(rel, old, new)
    probs = problems(pack)
    line = pack.line_of(rel, where_needle)
    assert has(probs, f"{rel}:{line}:", text), probs


def test_hidden_action_that_can_never_be_found(pack):
    pack.replace("locations/fountain.yaml",
                 '    inspect_hint: "Coins glint in the basin, as if people have wished here before."\n', "")
    probs = problems(pack)
    assert has(probs, f"locations/fountain.yaml:{pack.line_of('locations/fountain.yaml', '- id: make_wish')}:",
               "hidden but can never be discovered")


def test_last_outcome_must_be_the_fallback(pack):
    pack.replace("locations/fountain.yaml", '      - text: "The wishes are still there, waiting."',
                 '      - when: {random_lt: 0.5}\n        text: "The wishes are still there, waiting."')
    assert has(problems(pack), "locations/fountain.yaml:", "the last outcome must have no 'when'")


def test_duplicate_keys_and_bad_yaml(pack):
    pack.replace("locations/market.yaml", "sheltered: true", "sheltered: true\nsheltered: false")
    pack.write("locations/broken.yaml", "id: broken\nname: [unclosed\n")
    probs = problems(pack)
    assert has(probs, f"locations/market.yaml:{pack.line_of('locations/market.yaml', 'sheltered: false')}:",
               "duplicate key 'sheltered'")
    assert has(probs, "locations/broken.yaml:", "invalid YAML")


def test_duplicate_action_ids_across_files(pack):
    pack.write("locations/zoo.yaml", """\
id: zoo
name: the zoo
description: "x"
exits: [fountain]
actions:
  - {id: eat_offering, name: x, description: x, family: eat, discovery: known_on_arrival,
     duration: {minutes: [1, 2]}, on_finish: {creature: {hunger: -0.1}}}
""")
    assert has(problems(pack), "locations/zoo.yaml:6:", "already defined in market.yaml")


def test_builtins_are_required(pack):
    text = pack.read("actions/universal.yaml")
    pack.write("actions/universal.yaml", text[:text.index("- id: sleep")])
    probs = problems(pack)
    assert has(probs, "actions/universal.yaml:", "must include builtin 'sleep'")
    assert has(probs, "character.yaml:", "innate action 'sleep' does not exist")


def test_innate_actions_must_be_known_from_birth(pack):
    pack.replace("character.yaml", "innate_knowledge: [move, inspect, sleep]", "innate_knowledge: [move, inspect]")
    assert has(problems(pack), "character.yaml:", "'sleep' is innate but not in innate_knowledge")
