"""Repro harness for slidesmith candidate bugs."""
import json

# ---------- A. zero-height line ----------
from extraslide.content_requests import _create_line_request, generate_batch_requests, _order_deletes_for_safe_removal, _create_shape_request
r = _create_line_request("line1", "slide1", {"x": 100, "y": 200, "w": 300, "h": 0})
print("A. createLine for h=0:")
print(json.dumps(r["createLine"]["elementProperties"]["size"], indent=None))
print()

# shape with h=0 -> scaleY 0
s = _create_shape_request("r1", "slide1", "RECTANGLE", {"x": 0, "y": 0, "w": 100, "h": 0})
print("A2. createShape h=0 transform:", s["createShape"]["elementProperties"]["transform"])
print()

# ---------- B. text index corruption from dropped empty paragraph ----------
from extraslide.slide_processor import process_presentation
from extraslide.content_parser import parse_slide_content
from extraslide.content_diff import diff_presentation

def text_el(oid, paras):
    tes = []
    idx = 0
    for p in paras:
        content = p + "\n"
        tes.append({"paragraphMarker": {"style": {}}, "startIndex": idx, "endIndex": idx+len(content)})
        tes.append({"textRun": {"content": content, "style": {}}, "startIndex": idx, "endIndex": idx+len(content)})
        idx += len(content)
    return {
        "objectId": oid,
        "size": {"width": {"magnitude": 3000024, "unit": "EMU"}, "height": {"magnitude": 3000024, "unit": "EMU"}},
        "transform": {"scaleX": 1, "scaleY": 1, "translateX": 0, "translateY": 0, "unit": "EMU"},
        "shape": {"shapeType": "TEXT_BOX", "text": {"textElements": tes}},
    }

pres = {
    "presentationId": "P1",
    "title": "t",
    "pageSize": {"width": {"magnitude": 9144000, "unit": "EMU"}, "height": {"magnitude": 5143500, "unit": "EMU"}},
    "slides": [{"objectId": "slideA", "pageElements": [text_el("box1", ["Title", "", "Body"])]}],
}
result = process_presentation(pres)
pristine_sml = result["slides"][0]["content"]
print("B. pristine SML generated from remote text 'Title\\n\\nBody\\n':")
print(pristine_sml)
edited_sml = pristine_sml.replace("Body", "Bodyz")
diff = diff_presentation(
    {"01": parse_slide_content(pristine_sml)},
    {"01": parse_slide_content(edited_sml)},
    result["styles"], result["id_mapping"],
)
reqs = generate_batch_requests(diff, result["id_mapping"], {"01": "slideA"})
print("B. requests:", json.dumps(reqs))
remote_text = "Title\n\nBody\n"
for rq in reqs:
    if "insertText" in rq:
        i = rq["insertText"]["insertionIndex"]
        print(f"B. insert at {i}: remote text becomes -> {remote_text[:i] + rq['insertText']['text'] + remote_text[i:]!r} (expected 'Title\\n\\nBodyz\\n')")
print()

# ---------- B2. leading-space paragraph ----------
pres2 = {
    "presentationId": "P2", "title": "t",
    "pageSize": {"width": {"magnitude": 9144000, "unit": "EMU"}, "height": {"magnitude": 5143500, "unit": "EMU"}},
    "slides": [{"objectId": "slideB", "pageElements": [text_el("box2", ["  Hello world", "Second line"])]}],
}
res2 = process_presentation(pres2)
sml2 = res2["slides"][0]["content"]
print("B2. pristine SML for remote '  Hello world\\nSecond line\\n':")
print(sml2)
ed2 = sml2.replace("Second line", "Second edited")
d2 = diff_presentation({"01": parse_slide_content(sml2)}, {"01": parse_slide_content(ed2)}, res2["styles"], res2["id_mapping"])
rq2 = generate_batch_requests(d2, res2["id_mapping"], {"01": "slideB"})
print("B2. requests:", json.dumps(rq2))
remote2 = "  Hello world\nSecond line\n"
for rq in rq2:
    if "deleteText" in rq:
        tr = rq["deleteText"]["textRange"]
        print(f"B2. delete range {tr['startIndex']}..{tr['endIndex']} of real remote text deletes {remote2[tr['startIndex']:tr['endIndex']]!r}")
print()

# ---------- C. MOVE resets scale ----------
def shape_el(oid, scale_x=0.5, scale_y=0.5):
    return {
        "objectId": oid,
        "size": {"width": {"magnitude": 3000024, "unit": "EMU"}, "height": {"magnitude": 3000024, "unit": "EMU"}},
        "transform": {"scaleX": scale_x, "scaleY": scale_y, "translateX": 914400, "translateY": 914400, "unit": "EMU"},
        "shape": {"shapeType": "RECTANGLE", "shapeProperties": {}},
    }
pres3 = {
    "presentationId": "P3", "title": "t",
    "pageSize": {"width": {"magnitude": 9144000, "unit": "EMU"}, "height": {"magnitude": 5143500, "unit": "EMU"}},
    "slides": [{"objectId": "slideC", "pageElements": [shape_el("rect9")]}],
}
res3 = process_presentation(pres3)
sml3 = res3["slides"][0]["content"]
print("C. pristine SML (visual size 118.11pt, google scale 0.5):")
print(sml3)
ed3 = sml3.replace('x="72"', 'x="100"')
d3 = diff_presentation({"01": parse_slide_content(sml3)}, {"01": parse_slide_content(ed3)}, res3["styles"], res3["id_mapping"])
rq3 = generate_batch_requests(d3, res3["id_mapping"], {"01": "slideC"})
print("C. move request:", json.dumps(rq3))
print("C. NOTE: element's actual google transform scale was 0.5; ABSOLUTE scaleX/scaleY=1 => rendered size jumps from 118pt to 236pt")
print()

# resize attempt: change w
ed3b = sml3.replace('w="118.11"', 'w="200"')
d3b = diff_presentation({"01": parse_slide_content(sml3)}, {"01": parse_slide_content(ed3b)}, res3["styles"], res3["id_mapping"])
rq3b = generate_batch_requests(d3b, res3["id_mapping"], {"01": "slideC"})
print("C2. resize (w 118.11->200) request:", json.dumps(rq3b))
print()

# ---------- D. delete ordering ----------
print("D. delete wrapper+children created by prior copy push (google ids 'copy_01_5_1' + '_c0_*'):")
out = _order_deletes_for_safe_removal({"copy_01_5_1", "copy_01_5_1_c0_0", "copy_01_5_1_c0_1"})
print("   ordered deletes:", out, " <- wrapper id missing =>", "copy_01_5_1" not in out)
print()

# ---------- E. copy children double translation ----------
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
print("E. pristine SML (label nested in card by bounds containment):")
print(sml4)
# author duplicates the card at +300pt, moving children to final positions too
# full-copy legacy style: same ids, everything at new position
copy_block = """  <Rect id="e1" x="300" y="0" w="200" h="200">
    <TextBox id="e2" x="310" y="10" w="50" h="25">
      <P>hi</P>
    </TextBox>
  </Rect>
</Slide>"""
ed4 = sml4.replace("</Slide>", copy_block)
d4 = diff_presentation({"01": parse_slide_content(sml4)}, {"01": parse_slide_content(ed4)}, res4["styles"], res4["id_mapping"])
for c in d4.changes:
    print("E. change:", c.change_type, c.target_id, "translation:", c.translation, "children:", c.children)
rq4 = generate_batch_requests(d4, res4["id_mapping"], {"01": "slideD"})
for rq in rq4:
    if "createShape" in rq:
        t = rq["createShape"]["elementProperties"]["transform"]
        print("E. create", rq["createShape"]["objectId"], "at translate", t["translateX"]/12700, t["translateY"]/12700, "pt")
