# tests/software_tests/test_node_list.py
# The Pi's own node list (raspberrypi/nodes.json, not in git): starting it,
# saving it, the add-a-node questions, and what the dashboard is given.
import json
import sqlite3

import pytest

import nodes
import node_setup
import node_wizard
from nodes import NodeConfigError, load_nodes, next_free_id, public_nodes, read_node_file


@pytest.fixture
def pi_list(tmp_path, monkeypatch):
    '''Point the Pi's list, the example and the old repo-root file at tmp_path.'''
    monkeypatch.setattr(nodes, "NODES_FILE", tmp_path / "raspberrypi" / "nodes.json")
    monkeypatch.setattr(nodes, "EXAMPLE_FILE", nodes.EXAMPLE_FILE)   # the real example
    monkeypatch.setattr(nodes, "LEGACY_FILE", tmp_path / "nodes.json")
    (tmp_path / "raspberrypi").mkdir()
    return tmp_path


def node(name="Old", board="feather_m0"):
    return {"name": name, "framework": "arduino", "board": board, "sense_interval_s": 3,
            "log_interval_s": 300, "storage": "sd",
            "pins": {"radio_cs": 9, "radio_irq": 6, "radio_rst": 11, "sd_cs": 10}}


# ── Starting and saving the list ─────────────────────────────────────────────

def test_a_fresh_pi_starts_its_list_from_the_example(pi_list):
    assert list(load_nodes()) == [1, 2]
    assert nodes.NODES_FILE.exists()
    assert json.loads(nodes.NODES_FILE.read_text()) == json.loads(nodes.EXAMPLE_FILE.read_text())


def test_the_old_repo_root_list_is_carried_over_if_its_still_there(pi_list):
    nodes.LEGACY_FILE.write_text(json.dumps({"nodes": {"7": node("Kept")}}))
    assert load_nodes()[7]["name"] == "Kept"


def test_an_existing_list_is_never_replaced(pi_list):
    nodes.NODES_FILE.write_text(json.dumps({"nodes": {"5": node("Mine")}}))
    nodes.LEGACY_FILE.write_text(json.dumps({"nodes": {"7": node("Old")}}))
    assert list(load_nodes()) == [5]


def test_other_files_are_never_started_from_the_example(tmp_path, pi_list):
    with pytest.raises(NodeConfigError, match="Could not read"):
        load_nodes(tmp_path / "bench.json")
    assert read_node_file(tmp_path / "bench.json") == {"nodes": {}}
    assert not (tmp_path / "bench.json").exists()


def test_saving_keeps_the_about_text_and_sorts_by_id(pi_list):
    data = read_node_file()
    data["nodes"]["10"] = node("Ten")
    data["nodes"]["3"] = node("Three")
    nodes.save_node_file(data)
    saved = json.loads(nodes.NODES_FILE.read_text())
    assert list(saved["nodes"]) == ["1", "2", "3", "10"]
    assert saved["_about"] == data["_about"]
    assert [p.name for p in nodes.NODES_FILE.parent.iterdir()] == ["nodes.json"]   # no temp files left


def test_next_free_id():
    assert next_free_id([1, 2]) == 3
    assert next_free_id([1, 2, 4, 9]) == 3
    assert next_free_id([]) == 1
    assert next_free_id(range(1, 255)) is None


def test_the_dashboard_gets_names_and_colors_but_no_pins():
    shown = public_nodes(load_nodes(nodes.EXAMPLE_FILE))
    assert shown[1] == {"name": "Pico (CircuitPython)", "short": "Pico", "color": "#79c0ff",
                        "battery": {"type": "pw0", "label": "CAR BATTERY"},
                        "board": "pico2w", "framework": "circuitpython", "link": "radio"}
    assert "pins" not in shown[2]


# ── The questions ────────────────────────────────────────────────────────────

class Typing:
    '''Answers the wizard's questions in order; "" takes the default.'''
    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []
        self.said = []

    def wizard(self):
        return node_wizard.Wizard(ask=self.ask, say=self.said.append)

    def ask(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"Unexpected question: {prompt}")
        return self.answers.pop(0)


def test_an_esp32_v2_defaults_to_a_wifi_node_with_the_next_free_id(pi_list):
    t = Typing(*[""] * 8)   # link, ID, name, short, storage, sense, RTC, save (no log: nothing to log to)
    node_id = node_wizard.add_node(nodes.NODES_FILE, t.wizard(), boards=["feather_esp32_v2"])
    assert node_id == 3
    added = load_nodes()[3]
    assert added == {"name": "ESP32 V2 Feather 3", "short": "ESP32", "color": "#d2a8ff",
                     "framework": "arduino", "board": "feather_esp32_v2", "link": "wifi",
                     "sense_interval_s": 3, "log_interval_s": 60, "storage": "none"}
    # Board and framework have only one choice each, so aren't asked.
    assert "Board: ESP32 V2 Feather (feather_esp32_v2)" in [s.strip() for s in t.said]


def test_an_id_with_old_readings_is_skipped(pi_list):
    db_file = pi_list / "sensors.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE readings (ts TEXT, node_id INTEGER, sensor_type TEXT, key TEXT, value REAL)")
    conn.executemany("INSERT INTO readings VALUES ('t', ?, 's', 'k', 1)", [(3,), (4,)])
    conn.commit()
    conn.close()
    t = Typing("", "3", "", *[""] * 6)   # tries ID 3 (refused), then takes the default (5)
    node_id = node_wizard.add_node(nodes.NODES_FILE, t.wizard(), ["feather_esp32_v2"], db_file)
    assert node_id == 5
    assert any("old readings" in s for s in t.said)


def test_a_circuitpython_pico_on_the_radio_with_its_own_pins(pi_list):
    t = Typing(
        "",            # board: Pico 2 W (the default)
        "",            # framework: CircuitPython
        "",            # node ID 3
        "Bed 3",       # name
        "",            # short: "Bed"
        "",            # storage: SD (the default for a board with an SD pin)
        "n",           # don't use the usual pins
        "", "", "", "GP5", "", "",   # spi_sck, spi_mosi, spi_miso, radio_cs, radio_rst, sd_cs
        "", "",        # sense, log
        "y",           # RTC
        "7pm",         # sleep window: wrong form, asked again
        "19:00-07:00",
        "",            # save
    )
    node_id = node_wizard.add_node(nodes.NODES_FILE, t.wizard())
    added = load_nodes()[node_id]
    assert node_id == 3
    assert added["framework"] == "circuitpython" and added["board"] == "pico2w"
    assert added["short"] == "Bed" and added["storage"] == "sd" and added["rtc"] == "pcf8523"
    assert added["pins"] == {"spi_sck": "GP18", "spi_mosi": "GP19", "spi_miso": "GP16",
                             "radio_cs": "GP5", "radio_rst": "GP27", "sd_cs": "GP17"}
    assert added["sleep_window"] == ["19:00", "07:00"]
    assert added["log_interval_s"] == 300


def test_the_m0_is_offered_its_flash_chip_and_defaults_to_its_sd_card(pi_list):
    t = Typing("", "", "", "", "", "", "", "", "", "")
    node_id = node_wizard.add_node(nodes.NODES_FILE, t.wizard(), ["feather_m0"])
    added = load_nodes()[node_id]
    assert added["storage"] == "sd"
    assert added["pins"] == {"radio_cs": 9, "radio_irq": 6, "radio_rst": 11, "sd_cs": 10}
    assert any("flash chip" in s for s in t.said)


def test_q_stops_without_saving(pi_list):
    before = read_node_file()
    t = Typing("", "q")
    assert node_wizard.add_node(nodes.NODES_FILE, t.wizard(), ["feather_esp32_v2"]) is None
    assert read_node_file() == before


def test_q_at_the_review_saves_nothing(pi_list):
    before = read_node_file()
    t = Typing(*[""] * 7, "q")
    assert node_wizard.add_node(nodes.NODES_FILE, t.wizard(), ["feather_esp32_v2"]) is None
    assert read_node_file() == before


def test_b_goes_back_a_question_keeping_the_answers_as_defaults(pi_list):
    t = Typing(
        "",          # link: Wi-Fi
        "",          # ID 3
        "Kitchen",   # name
        "b",         # (short name) back to the name...
        "",          # ...which now defaults to "Kitchen"
        "b", "b",    # (short name) back to the name, back to the ID
        "7",         # ID
        "", "",      # name (still Kitchen), short
        "", "", "", "")   # storage, sense, RTC, save
    node_id = node_wizard.add_node(nodes.NODES_FILE, t.wizard(), ["feather_esp32_v2"])
    assert node_id == 7
    assert load_nodes()[7]["name"] == "Kitchen"
    assert "  Name (shown on the dashboard) [Kitchen]: " in t.prompts


def test_b_from_the_review_keeps_going_back_through_the_questions(pi_list):
    t = Typing(*[""] * 7,
               "b",         # at the review: back to the RTC question...
               "b",         # ...back again to the sense interval...
               "b",         # ...and to where it logs
               "1", "5",    # SD card, sd_cs on pin 5
               "", "",      # sense, log
               "",          # RTC
               "")          # save at the review
    node_id = node_wizard.add_node(nodes.NODES_FILE, t.wizard(), ["feather_esp32_v2"])
    added = load_nodes()[node_id]
    assert added["storage"] == "sd" and added["pins"] == {"sd_cs": 5}
    assert t.prompts[8].startswith("  Does it have a PCF8523")
    assert t.prompts[9].startswith("  Seconds between sensor readings")
    assert t.prompts[10] == "  Choose [2]: "


def test_b_at_the_first_question_stays_there(pi_list):
    t = Typing("b", *[""] * 8)
    assert node_wizard.add_node(nodes.NODES_FILE, t.wizard(), ["feather_esp32_v2"]) == 3
    assert any("first question" in s for s in t.said)


def test_changing_one_answer_at_the_review(pi_list):
    t = Typing(*[""] * 7,
               "4", "9",   # at the review: change answer 4 (the node ID) to 9
               "")         # back at the review: save
    assert node_wizard.add_node(nodes.NODES_FILE, t.wizard(), ["feather_esp32_v2"]) == 9
    assert load_nodes()[9]["name"] == "ESP32 V2 Feather 3"   # the name was already answered


def test_changing_where_it_logs_asks_the_questions_that_depend_on_it(pi_list):
    t = Typing(*[""] * 7,
               "7", "1",   # at the review: change answer 7 (where it logs) to the SD card...
               "4",        # ...which needs an SD pin
               "", "120", "",   # sense, log interval (now asked), RTC
               "")         # save
    node_id = node_wizard.add_node(nodes.NODES_FILE, t.wizard(), ["feather_esp32_v2"])
    added = load_nodes()[node_id]
    assert added["storage"] == "sd" and added["pins"] == {"sd_cs": 4} and added["log_interval_s"] == 120


def test_removing_a_node(pi_list):
    load_nodes()
    assert not node_wizard.remove_node(nodes.NODES_FILE, 2, Typing("").wizard())   # default: no
    assert list(load_nodes()) == [1, 2]
    assert node_wizard.remove_node(nodes.NODES_FILE, 2, Typing("y").wizard())
    assert list(load_nodes()) == [1]
    with pytest.raises(NodeConfigError, match="only node"):
        node_wizard.remove_node(nodes.NODES_FILE, 1, Typing("y").wizard())
    with pytest.raises(NodeConfigError, match="isn't in"):
        node_wizard.remove_node(nodes.NODES_FILE, 9, Typing("y").wizard())


# ── node_setup.py ────────────────────────────────────────────────────────────

def test_which_board_it_is_from_what_shows_up():
    assert node_setup.boards_with(chip="rp2350") == ["pico2", "pico2w"]
    assert node_setup.boards_with(circuitpython="raspberry_pi_pico_w") == ["picow"]
    assert node_setup.boards_with(circuitpython="something_else") is None


def test_circuitpython_board_id_from_boot_out(tmp_path):
    (tmp_path / "boot_out.txt").write_text(
        "Adafruit CircuitPython 10.3.1 on 2026-09-01; Raspberry Pi Pico 2 with rp2350a\n"
        "Board ID:raspberry_pi_pico2\nUID:ABCDEF\n")
    assert node_setup.circuitpython_board(tmp_path) == "raspberry_pi_pico2"
    assert node_setup.circuitpython_board(tmp_path / "missing") is None


def test_a_new_board_without_a_terminal_is_told_how_to_add_it(monkeypatch):
    monkeypatch.setattr(node_setup, "interactive", lambda: False)
    monkeypatch.setattr(node_setup, "load_nodes", lambda *a, **k: load_nodes(nodes.EXAMPLE_FILE))
    monkeypatch.setattr(node_setup, "detect", lambda args: node_setup.Board("bootloader", label="RPI-RP2"))
    import types
    a = types.SimpleNamespace(node=None, nodes="nodes.json", dry_run=False, install=False,
                              wifi_only=False, port=None)
    with pytest.raises(node_setup.SetupError, match="node_setup.py add"):
        node_setup.run(a)
