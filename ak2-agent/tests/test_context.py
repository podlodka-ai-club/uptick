import unittest
from ak2_agent.context import REFERENCE, context_history, deduplicate_context, observation_outline
from ak2_agent.context import ContextView, wire_size


class ContextTests(unittest.TestCase):
    def test_role_budgets_keep_instructions_and_current_state_despite_huge_history(self):
        skill='Full tool instructions. '*1600
        value={'knowledge':{'skills/operate/SKILL.md':skill,'prompts/world.md':'Mission',
                           'references/protocol.md':'large document'*10000},
               'observation':{'error':'invalid action','metrics':{'score':7}},
               'recent_events':[{'data':'unique'+str(i)+'x'*40000} for i in range(60)],
               'final_reflection':False,'can_adapt':True}
        result=ContextView(value).decision_preview()
        self.assertEqual(result['knowledge']['skills/operate/SKILL.md'],skill)
        self.assertEqual(result['observation'],value['observation'])
        self.assertFalse(result['knowledge']['references/protocol.md']['complete'])
        self.assertLess(wire_size(result),380000)

    def test_large_unicode_context_has_bounded_preview_and_exact_pages(self):
        original = {"observation": {"rows": [{"text": "Привет🌍"*1000, "id": i} for i in range(200)]},
                    "knowledge": {"references/a~b.md": "Правило\\\"\n"*10000}, "done": False}
        view = ContextView(original)
        self.assertLess(wire_size(view.preview()), 100000)
        pointer = "/knowledge/references~1a~0b.md"
        offset, parts = 0, []
        while True:
            page = view.read(pointer, offset)
            self.assertLess(wire_size(page), 200000)
            parts.append(page["content"])
            if page["next_offset"] is None: break
            offset = page["next_offset"]
        self.assertEqual("".join(parts), original["knowledge"]["references/a~b.md"])
        self.assertEqual(view.read('/observation/rows/199/id')['content'], 199)
        self.assertEqual(len(original['observation']['rows']), 200)

    def test_paging_collections_is_complete_and_rejects_invalid_paths(self):
        view = ContextView({"rows": list(range(27))})
        values, offset = [], 0
        while True:
            page = view.read('/rows', offset)
            values.extend(x['value'] for x in page['content'])
            if page['next_offset'] is None: break
            offset = page['next_offset']
        self.assertEqual(values, list(range(27)))
        for pointer, offset in [('/rows/-1',0),('/rows/01',0),('/bad~2',0),('/rows',-1),('/rows',True)]:
            with self.subTest(pointer=pointer, offset=offset), self.assertRaises(ValueError):
                view.read(pointer,offset)
        with self.assertRaises(KeyError): view.read('/etc/passwd')

    def test_exact_references_are_lossless_and_point_to_earlier_values(self):
        import json
        record = {"result": "Verified result. "*80, "unique_id": "op-1"}
        value = {"first/record~": record, "facts": [record, {"result":record["result"],"unique_id":"op-2"}]}
        compact = deduplicate_context(value)
        self.assertEqual(compact["facts"][0],{REFERENCE:"/first~1record~0"})
        self.assertEqual(compact["facts"][1]["unique_id"],"op-2")
        def expand(node):
            if isinstance(node, dict):
                if set(node)=={REFERENCE}:
                    target=compact
                    for part in node[REFERENCE].split("/")[1:]:
                        key=part.replace("~1","/").replace("~0","~")
                        target=target[int(key)] if isinstance(target,list) else target[key]
                    return expand(target)
                return {k:expand(v) for k,v in node.items()}
            return [expand(v) for v in node] if isinstance(node,list) else node
        self.assertEqual(expand(compact),value)
        self.assertLess(len(json.dumps(compact)),len(json.dumps(value))//2)

    def test_world_supplied_reference_marker_disables_deduplication(self):
        value={"data":{REFERENCE:"untrusted"},"repeated":["a"*600]*2}
        self.assertEqual(deduplicate_context(value),value)

    def test_history_retains_action_receipts_and_measurements_without_catalog_copies(self):
        events = [{"id": 1, "kind": "observation", "data": {
            "data": {"catalog": ["large schema"] * 100}, "metrics": {"score": 3},
            "error": {"source": "optional", "message": "timeout"}}},
            {"id": 2, "kind": "outcome", "data": {"response": {"new_id": "needed-next"}}}]
        result = context_history(events)
        self.assertEqual(result[0]["data"]["metrics"], {"score": 3})
        self.assertEqual(result[0]["data"]["error"], events[0]["data"]["error"])
        self.assertNotIn("catalog", str(result[0]["data"].get("data", {})))
        self.assertEqual(result[1], events[1])
        self.assertEqual(len(events[0]["data"]["data"]["catalog"]), 100)

    def test_exposes_old_bounded_collection_beside_current_clock(self):
        value={"data":{"clock":{"now":"2030-01-02T12:00:00Z"},
            "events":{"rows":[{"at":"2030-01-01T01:00:00Z"},{"at":"2030-01-01T00:00:00Z"}],"next_cursor":"next"}}}
        result=observation_outline(value)
        self.assertEqual(result["date_fields"][0]["value"],"2030-01-02T12:00:00Z")
        self.assertEqual(result["collections"][0]["date_ranges"]["at"]["last"],"2030-01-01T01:00:00Z")
        self.assertEqual(result["collections"][0]["pagination"],{"next_cursor":"next"})
        self.assertEqual(len(value["data"]["events"]["rows"]),2)
