"""Repro E redo: copy with duplicate ids (legacy detection) - child translation."""
import json
from extraslide.slide_processor import process_presentation
from extraslide.content_parser import parse_slide_content
from extraslide.content_diff import diff_presentation
from extraslide.content_requests import generate_batch_requests

pres4_children = [
    {
        "objectId": "cardX",
        "size": {"width": {"magnitude": 2540000, "unit": "EMU"}, "height": {"magnitude": 2540000, "unit": "EMU"}},
        "transform": {"scaleX": 1, "scaleY": 1, "translateX": 0, "translateY": 0, "unit": "EMU"},
        "shape": {"shapeType": "RECTANGLE", "shapeProperties": {}},
    },
    {
        "objectId": "labelX",
        "size": {"width": {"magnitude": 635000, "unit": "EMU"}, "height": {"magnitude": 317500, "unit": "EMU"}},
        "transform": {"scaleX": 1, "scaleY": 1, "translateX": 127000, "translateY": 127000, "unit": "EMU"},
        "shape": {"shapeType": "TEXT_BOX", "shapeProperties": {}, "text": {"textElements": [
            {"paragraphMarker": {"style": {}}}, {"textRun": {"content": "hi\n", "style": {}}}]}},
    },
]
pres4 = {
    "presentationId": "P4", "title": "t",
    "pageSize": {"width": {"magnitude": 9144000, "unit": "EMU"}, "height": {"magnitude": 5143500, "unit": "EMU"}},
    "slides": [{"objectId": "slideD", "pageElements": pres4_children}],
}
res4 = process_presentation(pres4)
sml4 = res4["slides"][0]["content"]

# Case 1: documented convention: copy root has x,y only (no w/h); children replicated verbatim (original positions)
copy_block_conv = """  <Rect id="cardX" x="300" y="0">
    <TextBox id="labelX" x="10" y="10" w="50" h="25">
      <P>hi</P>
    </TextBox>
  </Rect>
</Slide>"""
ed = sml4.replace("</Slide>", copy_block_conv)
d = diff_presentation({"01": parse_slide_content(sml4)}, {"01": parse_slide_content(ed)}, res4["styles"], res4["id_mapping"])
print("Case1 (copy convention, children at ORIGINAL positions):")
for c in d.changes:
    print("  change:", c.change_type.value, c.target_id, "translation:", c.translation)
rq = generate_batch_requests(d, res4["id_mapping"], {"01": "slideD"})
for r in rq:
    for op, body in r.items():
        if op in ("createShape", "createLine", "createImage"):
            t = body["elementProperties"]["transform"]
            sz = body["elementProperties"]["size"]
            w = sz["width"]["magnitude"]*t["scaleX"]/12700
            print(f"  {op} {body['objectId']}: pos=({t['translateX']/12700},{t['translateY']/12700})pt visual_w={w:.1f}pt")

# Case 2: author naturally writes copied children at FINAL positions
copy_block_final = """  <Rect id="cardX" x="300" y="0">
    <TextBox id="labelX" x="310" y="10" w="50" h="25">
      <P>hi</P>
    </TextBox>
  </Rect>
</Slide>"""
ed2 = sml4.replace("</Slide>", copy_block_final)
d2 = diff_presentation({"01": parse_slide_content(sml4)}, {"01": parse_slide_content(ed2)}, res4["styles"], res4["id_mapping"])
print("Case2 (children written at FINAL positions):")
rq2 = generate_batch_requests(d2, res4["id_mapping"], {"01": "slideD"})
for r in rq2:
    for op, body in r.items():
        if op in ("createShape", "createLine", "createImage"):
            t = body["elementProperties"]["transform"]
            print(f"  {op} {body['objectId']}: pos=({t['translateX']/12700},{t['translateY']/12700})pt")

# Case 3: does the COPY root itself get created? Where's the root createShape?
print("Case1 all requests:")
print(json.dumps(rq, indent=1))

# Case 4: user edits ORIGINAL label text while also copying the card -> edit dropped?
ed3 = sml4.replace("<P>hi</P>", "<P>changed</P>").replace("</Slide>", copy_block_conv)
d3 = diff_presentation({"01": parse_slide_content(sml4)}, {"01": parse_slide_content(ed3)}, res4["styles"], res4["id_mapping"])
print("Case4 (original label text edited AND card copied):")
for c in d3.changes:
    print("  change:", c.change_type.value, c.target_id, "new_text:", c.new_text)
