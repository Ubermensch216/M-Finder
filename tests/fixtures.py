"""Builders for small, realistic sample documents used by the viewer tests."""

from __future__ import annotations

import os
import struct
import sys
import zipfile
import zlib

sys.path.insert(0, os.path.dirname(__file__))

from test_preview import build_ole  # noqa: E402


def png(w: int = 40, h: int = 24, rgb=(220, 60, 60)) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# ---- docx ------------------------------------------------------------------------------

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" ' \
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" ' \
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" ' \
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'


def make_docx(path: str) -> None:
    styles = f'''<w:styles {W}><w:docDefaults><w:rPrDefault><w:rPr><w:sz w:val="21"/></w:rPr></w:rPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>
<w:pPr><w:spacing w:before="240" w:after="120"/></w:pPr><w:rPr><w:b/><w:color w:val="1F3864"/><w:sz w:val="40"/></w:rPr></w:style>
</w:styles>'''
    numbering = f'''<w:numbering {W}><w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/>
<w:lvlText w:val="%1."/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl></w:abstractNum>
<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/><w:lvlText w:val="&#xF0B7;"/>
<w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl></w:abstractNum>
<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num><w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num></w:numbering>'''
    body = f'''<w:document {W}><w:body>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>분기 보고서</w:t></w:r></w:p>
<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:rPr><w:b/><w:i/></w:rPr><w:t>굵고 기울인</w:t></w:r><w:r><w:rPr><w:color w:val="C00000"/><w:u w:val="single"/></w:rPr><w:t xml:space="preserve"> 빨간 밑줄 </w:t></w:r><w:r><w:rPr><w:highlight w:val="yellow"/></w:rPr><w:t>형광펜</w:t></w:r></w:p>
<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>첫째 항목</w:t></w:r></w:p>
<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>둘째 항목</w:t></w:r></w:p>
<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="2"/></w:numPr></w:pPr><w:r><w:t>글머리 기호</w:t></w:r></w:p>
<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/></w:tblPr><w:tblGrid><w:gridCol w:w="2400"/><w:gridCol w:w="2400"/><w:gridCol w:w="2400"/></w:tblGrid>
<w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/><w:shd w:fill="D9E2F3"/></w:tcPr><w:p><w:r><w:rPr><w:b/></w:rPr><w:t>병합된 머리글</w:t></w:r></w:p></w:tc>
<w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr><w:p><w:r><w:t>세로 병합</w:t></w:r></w:p></w:tc></w:tr>
<w:tr><w:tc><w:p><w:r><w:t>A1</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>B1 긴 내용이 들어가면 셀 안에서 줄바꿈이 되어야 합니다</w:t></w:r></w:p></w:tc>
<w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/></w:tc></w:tr></w:tbl>
<w:p><w:r><w:t>그림:</w:t></w:r><w:r><w:drawing><wp:inline><wp:extent cx="1143000" cy="685800"/><a:graphic><a:graphicData><a:pic><a:blipFill><a:blip r:embed="rId5"/></a:blipFill></a:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>
<w:p><w:r><w:t>마지막 문단 needle 포함</w:t></w:r></w:p>
</w:body></w:document>'''
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/></Relationships>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", body)
        z.writestr("word/styles.xml", styles)
        z.writestr("word/numbering.xml", numbering)
        z.writestr("word/_rels/document.xml.rels", rels)
        z.writestr("word/media/image1.png", png(120, 72))


# ---- hwpx --------------------------------------------------------------------------------

def make_hwpx(path: str) -> None:
    header = '''<hh:head xmlns:hh="h" xmlns:hc="c"><hh:refList>
<hh:fontfaces><hh:fontface lang="HANGUL"><hh:font id="0" face="맑은 고딕"/></hh:fontface></hh:fontfaces>
<hh:borderFills><hh:borderFill id="1"><hh:leftBorder type="SOLID"/><hh:rightBorder type="SOLID"/><hh:topBorder type="SOLID"/><hh:bottomBorder type="SOLID"/><hc:fillBrush><hc:winBrush faceColor="#FFF2CC"/></hc:fillBrush></hh:borderFill>
<hh:borderFill id="2"><hh:leftBorder type="NONE"/></hh:borderFill></hh:borderFills>
<hh:charProperties><hh:charPr id="0" height="1000" textColor="#000000"><hh:fontRef hangul="0"/></hh:charPr>
<hh:charPr id="1" height="1600" textColor="#0000FF"><hh:fontRef hangul="0"/><hh:bold/><hh:underline type="BOTTOM"/></hh:charPr></hh:charProperties>
<hh:paraProperties><hh:paraPr id="0"><hh:align horizontal="JUSTIFY"/><hh:margin><hc:intent value="0"/><hc:left value="0"/><hc:prev value="0"/><hc:next value="300"/></hh:margin></hh:paraPr>
<hh:paraPr id="1"><hh:align horizontal="CENTER"/></hh:paraPr></hh:paraProperties></hh:refList></hh:head>'''
    sec = '''<hs:sec xmlns:hs="s" xmlns:hp="p" xmlns:hc="c">
<hp:p paraPrIDRef="1"><hp:run charPrIDRef="1"><hp:t>사업 계획서</hp:t></hp:run></hp:p>
<hp:p paraPrIDRef="0"><hp:run charPrIDRef="0"><hp:t>본문 문장입니다. </hp:t></hp:run><hp:run charPrIDRef="1"><hp:t>강조된 단어</hp:t></hp:run></hp:p>
<hp:p paraPrIDRef="0"><hp:run charPrIDRef="0"><hp:tbl rowCnt="2" colCnt="2" borderFillIDRef="1">
<hp:tr><hp:tc borderFillIDRef="1"><hp:subList><hp:p paraPrIDRef="0"><hp:run charPrIDRef="1"><hp:t>항목</hp:t></hp:run></hp:p></hp:subList><hp:cellAddr colAddr="0" rowAddr="0"/><hp:cellSpan colSpan="1" rowSpan="1"/><hp:cellSz width="9000" height="1000"/></hp:tc>
<hp:tc borderFillIDRef="1"><hp:subList><hp:p paraPrIDRef="0"><hp:run charPrIDRef="0"><hp:t>내용 needle</hp:t></hp:run></hp:p></hp:subList><hp:cellAddr colAddr="1" rowAddr="0"/><hp:cellSpan colSpan="1" rowSpan="1"/><hp:cellSz width="18000" height="1000"/></hp:tc></hp:tr>
<hp:tr><hp:tc borderFillIDRef="1"><hp:subList><hp:p paraPrIDRef="0"><hp:run charPrIDRef="0"><hp:t>예산</hp:t></hp:run></hp:p></hp:subList><hp:cellAddr colAddr="0" rowAddr="1"/><hp:cellSpan colSpan="1" rowSpan="1"/><hp:cellSz width="9000" height="1000"/></hp:tc>
<hp:tc borderFillIDRef="1"><hp:subList><hp:p paraPrIDRef="0"><hp:run charPrIDRef="0"><hp:t>1,000,000원</hp:t></hp:run></hp:p></hp:subList><hp:cellAddr colAddr="1" rowAddr="1"/><hp:cellSpan colSpan="1" rowSpan="1"/><hp:cellSz width="18000" height="1000"/></hp:tc></hp:tr>
</hp:tbl></hp:run></hp:p>
<hp:p paraPrIDRef="0"><hp:run charPrIDRef="0"><hp:pic><hp:sz width="8000" height="4800"/><hc:img binaryItemIDRef="image1"/></hp:pic></hp:run></hp:p>
</hs:sec>'''
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Contents/header.xml", header)
        z.writestr("Contents/section0.xml", sec)
        z.writestr("BinData/image1.png", png(80, 48, (40, 120, 220)))


# ---- xlsx --------------------------------------------------------------------------------

def make_xlsx(path: str) -> None:
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    wb = f'<workbook {ns}><sheets><sheet name="매출" sheetId="1" r:id="rId1"/><sheet name="메모" sheetId="2" r:id="rId2"/></sheets></workbook>'
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="x" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="x" Target="worksheets/sheet2.xml"/></Relationships>')
    styles = f'''<styleSheet {ns}><numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0&quot;원&quot;"/></numFmts>
<fonts count="2"><font><sz val="11"/></font><font><b/><sz val="12"/><color rgb="FFFFFFFF"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/></patternFill></fill></fills>
<borders count="2"><border/><border><left style="thin"/><right style="thin"/><top style="thin"/><bottom style="thin"/></border></borders>
<cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1"><alignment horizontal="center"/></xf>
<xf numFmtId="164" fontId="0" fillId="0" borderId="1"/><xf numFmtId="14" fontId="0" fillId="0" borderId="0"/><xf numFmtId="10" fontId="0" fillId="0" borderId="0"/></cellXfs></styleSheet>'''
    sst = f'<sst {ns}><si><t>월</t></si><si><t>매출액</t></si><si><t>비고</t></si><si><t>needle 항목</t></si></sst>'
    rows = ['<row r="1"><c r="A1" s="1" t="s"><v>0</v></c><c r="B1" s="1" t="s"><v>1</v></c><c r="C1" s="1" t="s"><v>2</v></c></row>']
    for i in range(2, 40):
        rows.append(f'<row r="{i}"><c r="A{i}" s="3"><v>{45000 + i}</v></c><c r="B{i}" s="2"><v>{i * 123456}</v></c><c r="C{i}" s="4"><v>0.{i:02d}</v></c></row>')
    rows.append('<row r="41"><c r="A41" t="s"><v>3</v></c></row>')
    sheet1 = (f'<worksheet {ns}><sheetFormatPr defaultRowHeight="15"/><cols><col min="1" max="1" width="14" customWidth="1"/><col min="2" max="3" width="18" customWidth="1"/></cols>'
              f'<sheetData>{"".join(rows)}</sheetData><mergeCells count="1"><mergeCell ref="E1:G2"/></mergeCells></worksheet>')
    sheet2 = f'<worksheet {ns}><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>두 번째 시트</t></is></c></row></sheetData></worksheet>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/styles.xml", styles)
        z.writestr("xl/sharedStrings.xml", sst)
        z.writestr("xl/worksheets/sheet1.xml", sheet1)
        z.writestr("xl/worksheets/sheet2.xml", sheet2)


# ---- markdown ------------------------------------------------------------------------------

MARKDOWN = """# 프로젝트 안내

일반 문단에 **굵게**, *기울임*, `코드`, ~~취소선~~ 그리고 [링크](https://example.com) 가 있습니다.

## 할 일
- [x] 완료된 항목
- [ ] 남은 항목
  - 하위 항목 needle
1. 첫째
2. 둘째

> 인용문입니다.
> 두 번째 줄

| 이름 | 값 |
|:--|--:|
| 알파 | 1 |
| 베타 | 22 |

```python
def hello():
    print("hi")
```

---
끝.
"""


def make_md(path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(MARKDOWN)


HTML = """<!doctype html>
<html>
<head><title>Ignored</title><style>body { color: red; }</style></head>
<body>
<h1>HTML 보고서</h1>
<p>본문에는 <b>굵은 글씨</b>, <i>기울임</i>, <code>코드</code>, <a href="https://example.com">링크</a>가 포함됩니다.</p>
<ul>
  <li>첫 번째 항목</li>
  <li>두 번째 항목 needle</li>
</ul>
<table>
  <tr><th>항목</th><th>상태</th></tr>
  <tr><td>검토</td><td>완료</td></tr>
</table>
</body>
</html>
"""


def make_html(path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(HTML)


# ---- hwp5 -----------------------------------------------------------------------------------

def rec(tag: int, level: int, payload: bytes = b"") -> bytes:
    if len(payload) >= 0xFFF:
        return struct.pack("<II", tag | level << 10 | 0xFFF << 20, len(payload)) + payload
    return struct.pack("<I", tag | level << 10 | len(payload) << 20) + payload


def _ptext(text: str) -> bytes:
    return text.encode("utf-16-le") + struct.pack("<H", 13)


def _para(text: str, shape: int, cs: int, level: int, ctrls: bytes = b"", cs_runs=None) -> bytes:
    n = len(text) + 1
    out = rec(66, level, struct.pack("<IIHBBHHHI", n, 0, shape, 0, 0, 1, 0, 0, 0))
    out += rec(67, level + 1, _ptext(text)) if text else b""
    runs = cs_runs or [(0, cs)]
    out += rec(68, level + 1, b"".join(struct.pack("<II", p, i) for p, i in runs))
    return out + ctrls


def make_hwp(path: str) -> None:
    def charshape(size100, bold=False, color=0):
        d = struct.pack("<7H", *[0] * 7) + bytes([100] * 7) + bytes(7) + bytes([100] * 7) + bytes(7)
        d += struct.pack("<iI", size100, 2 if bold else 0) + b"\0\0" + struct.pack("<II", color, 0xFFFFFFFF) + b"\0" * 8
        return d

    def parashape(align):
        return struct.pack("<I", align << 2) + struct.pack("<iiiii", 0, 0, 0, 0, 600) + struct.pack("<I", 160) + b"\0" * 28

    def borderfill(bg=None):
        d = struct.pack("<H", 0) + b"".join(bytes([1, 1]) + struct.pack("<I", 0) for _ in range(4)) + bytes([0, 0]) + struct.pack("<I", 0)
        if bg is None:
            return d + struct.pack("<I", 0)
        return d + struct.pack("<II", 1, bg) + struct.pack("<Ii", 0, -1)

    docinfo = b"".join([
        rec(17, 0, struct.pack("<18I", 1, 1, 1, 0, 0, 0, 0, 0, 2, 3, 0, 0, 0, 2, 0, 0, 0, 0)),
        rec(18, 0, struct.pack("<HHH", 1, 1, 3) + "png".encode("utf-16-le")),
        rec(19, 0, bytes([0]) + struct.pack("<H", 6) + "맑은 고딕".encode("utf-16-le")),
        rec(20, 0, borderfill()), rec(20, 0, borderfill(0xCCE5FF)),
        rec(21, 0, charshape(1000)), rec(21, 0, charshape(2000, True, 0x0000FF)), rec(21, 0, charshape(1000, False, 0x2020C0)),
        rec(25, 0, parashape(1)), rec(25, 0, parashape(3)),
    ])
    cell_hdr = lambda col, row, cspan, rspan, bf: struct.pack("<HHIHHHHIIHHHHH", 1, 0, 0, col, row, cspan, rspan, 9000, 1000, 141, 141, 141, 141, bf)[:34]
    tbl = rec(71, 1, b" lbt" + b"\0" * 44)  # ctrl ids are stored byte-reversed
    tbl += rec(77, 2, struct.pack("<IHH", 0, 2, 2) + b"\0" * 20)
    for (c, r, txt, bf) in ((0, 0, "항목", 2), (1, 0, "내용 needle", 1), (0, 1, "예산", 1), (1, 1, "1,000,000원", 1)):
        tbl += rec(72, 2, cell_hdr(c, r, 1, 1, bf))
        tbl += _para(txt, 0, 0, 2)
    pic = rec(71, 1, b" osg" + struct.pack("<IiiII", 0, 0, 0, 6000, 3600) + b"\0" * 20)
    pic += rec(76, 2, b"\0" * 8) + rec(85, 3, b"\0" * 71 + struct.pack("<H", 1) + b"\0" * 4)
    body = b"".join([
        _para("보고서 제목", 1, 1, 0),
        _para("일반 본문과 빨간 글자가 섞인 문단입니다.", 0, 0, 0, cs_runs=[(0, 0), (10, 2), (14, 0)]),
        _para("", 0, 0, 0, ctrls=tbl),
        _para("", 0, 0, 0, ctrls=pic),
        _para("마지막 줄 needle", 0, 0, 0),
    ])

    def c_(b: bytes) -> bytes:
        o = zlib.compressobj(wbits=-15)
        return o.compress(b) + o.flush()

    header = b"HWP Document File".ljust(32, b"\0") + struct.pack("<II", 0x05000300, 1)
    data = build_ole({
        "FileHeader": header.ljust(256, b"\0"),
        "DocInfo": c_(docinfo),
        "BodyText/Section0": c_(body),
        "BinData/BIN0001.png": c_(png(100, 60, (60, 160, 90))),
    })
    with open(path, "wb") as f:
        f.write(data)


# ---- pdf ---------------------------------------------------------------------------------------

def make_pdf(path: str) -> bool:
    try:
        import pymupdf
    except Exception:
        return False
    doc = pymupdf.open()
    for i in range(3):
        page = doc.new_page()
        page.insert_text((72, 100), f"Page {i + 1} heading", fontsize=22)
        page.insert_text((72, 140), "The needle is on this page." if i == 1 else "Plain text here.", fontsize=12)
        page.draw_rect(pymupdf.Rect(72, 200, 300, 260), color=(0, 0, 1), fill=(0.8, 0.9, 1))
    doc.save(path)
    doc.close()
    return True


def make_all(folder: str) -> dict:
    os.makedirs(folder, exist_ok=True)
    out = {}
    for name, fn in (("sample.docx", make_docx), ("sample.hwpx", make_hwpx), ("sample.xlsx", make_xlsx),
                     ("sample.md", make_md), ("sample.html", make_html), ("sample.hwp", make_hwp), ("sample.pdf", make_pdf)):
        p = os.path.join(folder, name)
        if fn(p) is not False:
            out[name] = p
    with open(os.path.join(folder, "sample.txt"), "w", encoding="utf-8") as f:
        f.write("첫 줄\nneedle in a haystack\n끝\n")
    out["sample.txt"] = os.path.join(folder, "sample.txt")
    return out
