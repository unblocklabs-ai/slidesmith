import json
from extraslide.content_requests import _order_deletes_for_safe_removal
from extraslide.id_manager import assign_ids
from extraslide.client import SlidesClient
from extraslide.transport import Transport

# 1. Group + children delete ordering: pristine google ids (no _c pattern)
ids = {"gGROUP123", "gCHILDa", "gCHILDb"}
print("group delete order:", _order_deletes_for_safe_removal(ids))
print("  (all classified root shapes; relative order of group vs children is hash-dependent)")

# 2. authored element id 's2' shadowing slide clean ids
pres = {
    "presentationId": "P", "title": "t",
    "slides": [
        {"objectId": "SLIDES_API1", "pageElements": [
            {"objectId": "s2", "shape": {"shapeType": "RECTANGLE"},
             "size": {"width": {"magnitude": 1, "unit": "EMU"}, "height": {"magnitude": 1, "unit": "EMU"}},
             "transform": {"scaleX": 1, "scaleY": 1, "translateX": 0, "translateY": 0, "unit": "EMU"}},
        ]},
        {"objectId": "SLIDES_API2", "pageElements": []},
    ],
}
m = assign_ids(pres)
print("id_mapping:", m.to_dict())

class Dummy(Transport):
    async def get_presentation(self, _): raise NotImplementedError
    async def batch_update(self, *a, **k): raise NotImplementedError
    async def close(self): pass

c = SlidesClient(Dummy())
print("slide_id_mapping:", c._build_slide_id_mapping(m.to_dict()))
