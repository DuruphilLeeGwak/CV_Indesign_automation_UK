#!/usr/bin/env python3
"""
IDML 주입 스크립트 v9
me.toml (고정) + <Company>_<Position>.toml (회사별) → output/<Name>_<Company>_<Position>.idml

사용법:
  python inject_idml.py                          # 폴더에 toml이 1개일 때 자동 선택
  python inject_idml.py PrincipleHR_RealtimeVFXArtist   # 특정 toml 지정 (.toml 생략 가능)
"""

import sys
import zipfile
import shutil
import tomllib
from pathlib import Path
from lxml import etree

BASE          = Path(__file__).parent
ME            = BASE / "me.toml"
OUTPUT_DIR    = BASE / "output"
TEMPLATE_IDML = BASE / "template" / "WS_Template.idml"

OUTPUT_DIR.mkdir(exist_ok=True)

# 입력 toml 선택
# ① 인자가 있으면 그 이름의 toml 사용 (.toml 확장자 생략 가능)
# ② 인자가 없으면 me.toml 제외한 toml이 정확히 1개일 때 자동 선택
def _resolve_input():
    if len(sys.argv) > 1:
        name = sys.argv[1]
        if not name.endswith(".toml"):
            name += ".toml"
        path = BASE / name
        if not path.exists():
            available = [f.name for f in BASE.glob("*.toml") if f.name != "me.toml"]
            raise FileNotFoundError(
                f"'{name}' 파일을 찾을 수 없습니다.\n"
                f"사용 가능한 toml: {available}"
            )
        return path

    _toml_files = [f for f in BASE.glob("*.toml") if f.name != "me.toml"]
    if len(_toml_files) == 0:
        raise FileNotFoundError(
            "input toml 파일이 없습니다.\n"
            "예: FosterPartners_RealTimeArtist.toml"
        )
    if len(_toml_files) > 1:
        raise ValueError(
            f"toml 파일이 2개 이상입니다: {[f.name for f in _toml_files]}\n"
            "사용할 파일명을 인자로 지정하세요.\n"
            f"예: python inject_idml.py {_toml_files[0].stem}"
        )
    return _toml_files[0]


INPUT = _resolve_input()

NS   = "http://ns.adobe.com/AdobeInDesign/idml/1.0/packaging"
CHAR = "CharacterStyle/$ID/[No character style]"
HYPR = "CharacterStyle/$ID/Hyperlink"

STYLE = {
    "heading":   "ParagraphStyle/Sans Serif, Bold, 10 Pt, Tracking 100, All Caps, Paragraph Rule",
    "bold":      "ParagraphStyle/Sans Serif, Bold, 10 Pt",
    "italic":    "ParagraphStyle/Sans Serif, Italic, 10 Pt",
    "regular":   "ParagraphStyle/Sans Serif, Regular, 10 Pt",
    "name_p1":   "ParagraphStyle/Sans Serif, Light, 70 Pt, All Caps",
    "name_hdr":  "ParagraphStyle/Sans Serif, Light, 20 Pt, right aligned, All Caps",
    "initials":  "ParagraphStyle/Sans Serif, Bold, 70 Pt, All Caps",
    "signature": "ParagraphStyle/Signature",
}

HANG_LEFT  = "7"
HANG_FIRST = "-7"

_hyperlinks = []
_hl_counter = [10]


# ── XML 헬퍼 ──────────────────────────────────────

def make_ch(parent, style=None, font_style=None, point_size=None, fill_color=None):
    attrs = {"AppliedCharacterStyle": style or CHAR}
    if font_style:  attrs["FontStyle"] = font_style
    if point_size:  attrs["PointSize"] = str(point_size)
    if fill_color:  attrs["FillColor"] = fill_color
    return etree.SubElement(parent, "CharacterStyleRange", **attrs)


def content(parent, text):
    c = etree.SubElement(parent, "Content")
    c.text = text
    return c


def br(parent):
    etree.SubElement(parent, "Br")


def para(style_key, left_indent=None, first_indent=None,
         space_after=None, keep_with_next=False):
    attrs = {"AppliedParagraphStyle": STYLE[style_key]}
    if left_indent  is not None: attrs["LeftIndent"]      = left_indent
    if first_indent is not None: attrs["FirstLineIndent"] = first_indent
    if space_after  is not None: attrs["SpaceAfter"]      = space_after
    if keep_with_next:           attrs["KeepWithNext"]    = "true"
    return etree.Element("ParagraphStyleRange", **attrs)


def blank():
    p = para("regular")
    c = make_ch(p); br(c)
    return p


def make_hyperlink(parent, source_self, url, display_text):
    _hl_counter[0] += 1
    key = str(_hl_counter[0])
    c_hyp = make_ch(parent, style=HYPR, fill_color="Color/Hyperlink")
    hl = etree.SubElement(c_hyp, "HyperlinkTextSource",
                          Self=source_self,
                          Name=url,
                          Hidden="false",
                          AppliedCharacterStyle=HYPR)
    content(hl, display_text)
    _hyperlinks.append((source_self, url, display_text, key))
    return hl


def build_story(self_id, paragraphs):
    root = etree.Element(f"{{{NS}}}Story", DOMVersion="21.3")
    story = etree.SubElement(root, "Story",
                             Self=self_id,
                             AppliedTOCStyle="n",
                             UserText="true",
                             IsEndnoteStory="false",
                             TrackChanges="false",
                             StoryTitle="$ID/",
                             AppliedNamedGrid="n")
    etree.SubElement(story, "StoryPreference",
                     OpticalMarginAlignment="false",
                     OpticalMarginSize="12",
                     FrameType="TextFrameType",
                     StoryOrientation="Horizontal",
                     StoryDirection="LeftToRightDirection")
    etree.SubElement(story, "InCopyExportOption",
                     IncludeGraphicProxies="true",
                     IncludeAllResources="false")
    for p in paragraphs:
        story.append(p)
    return etree.tostring(root, xml_declaration=True,
                          encoding="UTF-8", pretty_print=True)


# ── designmap.xml 하이퍼링크 등록 ─────────────────

def patch_designmap(tmp_dir, hyperlinks):
    dm_path = tmp_dir / "designmap.xml"
    tree = etree.parse(str(dm_path))
    root = tree.getroot()

    children = list(root)
    last_hl_idx = 0
    for i, child in enumerate(children):
        tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
        if tag in ('Hyperlink', 'HyperlinkURLDestination'):
            last_hl_idx = i
    insert_idx = last_hl_idx + 1

    for source_self, url, display_text, key in hyperlinks:
        encoded = url.replace("https://", "https%3a//") \
                     .replace("http://",  "http%3a//")
        dest_self = f"HyperlinkURLDestination/{encoded}"

        exists_dest = any(
            c.get("DestinationUniqueKey") == key
            for c in root
            if (c.tag.split('}')[-1] if '}' in c.tag else c.tag) == 'HyperlinkURLDestination'
        )
        if not exists_dest:
            dest_elem = etree.Element("HyperlinkURLDestination",
                                      Self=dest_self,
                                      DestinationUniqueKey=key,
                                      Name=url,
                                      DestinationURL=url,
                                      Hidden="true")
            root.insert(insert_idx, dest_elem)
            insert_idx += 1

        exists_hl = any(
            c.get("Source") == source_self
            for c in root
            if (c.tag.split('}')[-1] if '}' in c.tag else c.tag) == 'Hyperlink'
        )
        if not exists_hl:
            hl = etree.Element("Hyperlink",
                               Self=f"hl_{key}",
                               Name=display_text,
                               Source=source_self,
                               Visible="false",
                               Highlight="None",
                               Width="Thin",
                               BorderStyle="Solid",
                               Hidden="false",
                               EpubAriaRole="",
                               HypherlinkAltText="",
                               DestinationUniqueKey=key)
            props = etree.SubElement(hl, "Properties")
            bc = etree.SubElement(props, "BorderColor")
            bc.set("type", "enumeration"); bc.text = "Black"
            dest_ref = etree.SubElement(props, "Destination")
            dest_ref.set("type", "object"); dest_ref.text = dest_self
            root.insert(insert_idx, hl)
            insert_idx += 1

    tree.write(str(dm_path), xml_declaration=True,
               encoding="UTF-8", pretty_print=True)
    print(f"  ✓ designmap.xml — 하이퍼링크 {len(hyperlinks)}개 등록")


# ── 섹션별 빌더 ───────────────────────────────────

def xml_initials(self_id, me, job):
    p = para("initials")
    c = make_ch(p); content(c, me["personal"]["initials"])
    return build_story(self_id, [p])


def xml_name_p1(self_id, me, job):
    p = para("name_p1")
    c = make_ch(p); content(c, me["personal"]["name"])
    return build_story(self_id, [p])


def xml_name_hdr(self_id, me, job):
    p = para("name_hdr")
    c = make_ch(p); content(c, me["personal"]["name"])
    return build_story(self_id, [p])


def xml_about(self_id, me, job):
    opening = job["about"]["opening"]
    body    = job["about"]["body"].strip()
    closing = job["about"]["closing"]
    p = para("regular")
    c = make_ch(p)
    content(c, opening + " " + body)
    br(c); br(c)
    content(c, closing)
    return build_story(self_id, [p])


def xml_contact(self_id, me, job):
    pi = me["personal"]
    p = para("regular")
    c = make_ch(p)
    content(c, "CONTACT");                               br(c)
    content(c, f"{pi['phone']} | {pi['email']}");        br(c)
    content(c, pi["location"]);                          br(c)
    if pi.get("linkedin"):
        make_hyperlink(p,
                       f"hl_linkedin_{self_id}",
                       pi.get("linkedin_url", pi["linkedin"]),
                       pi["linkedin"])
        c2 = make_ch(p); br(c2)
    make_hyperlink(p,
                   f"hl_portfolio_{self_id}",
                   pi.get("portfolio_url", pi["portfolio"]),
                   pi["portfolio"])
    return build_story(self_id, [p])


def xml_skills(self_id, me, job):
    order = job["skills_order"]["list"]
    label_map = {
        "engines":  "Engines",
        "realtime": "Real-Time",
        "code":     "Code",
        "vr_ar":    "VR / AR",
        "tools":    "Tools",
    }
    paras = []
    p = para("heading"); c = make_ch(p)
    content(c, "Skills"); br(c)
    paras.append(p)
    paras.append(blank())

    for key in order:
        label = label_map.get(key, key)
        items = [i.strip() for i in me["skills"][key].split("·") if i.strip()]
        p = para("regular")
        c_bold = make_ch(p, font_style="Bold")
        content(c_bold, label); br(c_bold)
        paras.append(p)
        for item in items:
            p = para("regular", left_indent="8", first_indent="0")
            c_light = make_ch(p, font_style="Light")
            content(c_light, item); br(c_light)
            paras.append(p)
        paras.append(blank())

    return build_story(self_id, paras)


def xml_education(self_id, me, job):
    paras = []
    p = para("heading"); c = make_ch(p)
    content(c, "Education"); br(c)
    paras.append(p)
    paras.append(blank())
    for e in me["education"]:
        loc = f", {e['location']}" if e.get("location") else ""
        p = para("bold"); c = make_ch(p)
        content(c, e["degree"]); br(c)
        paras.append(p)
        p = para("italic"); c = make_ch(p)
        content(c, f"{e['institution']}{loc}  {e['period']}"); br(c); br(c)
        paras.append(p)
    return build_story(self_id, paras)


def xml_languages(self_id, me, job):
    paras = []
    p = para("heading"); c = make_ch(p)
    content(c, "Languages"); br(c)
    paras.append(p)
    paras.append(blank())
    for l in me["languages"]:
        p = para("regular")
        c_bold = make_ch(p, font_style="Bold")
        content(c_bold, l["lang"] + "  ")
        c_reg = make_ch(p)
        content(c_reg, l["level"]); br(c_reg)
        paras.append(p)
    return build_story(self_id, paras)


def _work_exp_paras(exps, bullet_space, include_heading=True):
    """exp 리스트를 단락 리스트로 변환 (공통 헬퍼)"""
    paras = []
    if include_heading:
        p = para("heading"); c = make_ch(p)
        content(c, "Work Experience"); br(c)
        paras.append(p)
        paras.append(blank())
    for exp in exps:
        p = para("bold", keep_with_next=True)
        c = make_ch(p, point_size=12); content(c, exp["title"])
        c2 = make_ch(p); br(c2)
        paras.append(p)

        p = para("italic")
        c = make_ch(p, point_size=11)
        content(c, f"{exp['company']} · {exp['location']}"); br(c)
        paras.append(p)

        p = para("regular")
        c = make_ch(p, point_size=11); content(c, exp["period"])
        c2 = make_ch(p); br(c2); br(c2)
        content(c2, exp["intro"]); br(c2); br(c2)
        paras.append(p)

        for proj in exp.get("projects", []):
            p = para("italic", keep_with_next=True)
            c = make_ch(p, font_style="Bold")
            content(c, proj["name"])
            c2 = make_ch(p); br(c2)
            paras.append(p)

            for b in proj["bullets"]:
                p = para("regular",
                         left_indent=HANG_LEFT,
                         first_indent=HANG_FIRST,
                         space_after=bullet_space)
                c = make_ch(p)
                content(c, f"• {b}"); br(c)
                paras.append(p)
            paras.append(blank())
        paras.append(blank())
    return paras


def xml_work_commercial(self_id, me, job):
    bullet_space = job.get("layout", {}).get("bullet_space_after", "2")
    paras = []
    p = para("heading"); c = make_ch(p)
    content(c, "Work Experience"); br(c)
    paras.append(p)
    paras.append(blank())

    for exp in job["work_commercial"]:
        p = para("bold", keep_with_next=True)
        c = make_ch(p, point_size=12); content(c, exp["title"])
        c2 = make_ch(p); br(c2)
        paras.append(p)

        p = para("italic")
        c = make_ch(p, point_size=11)
        content(c, f"{exp['company']} · {exp['location']}"); br(c)
        paras.append(p)

        p = para("regular")
        c = make_ch(p, point_size=11); content(c, exp["period"])
        c2 = make_ch(p); br(c2); br(c2)
        content(c2, exp["intro"]); br(c2); br(c2)
        paras.append(p)

        for proj in exp.get("projects", []):
            p = para("italic", keep_with_next=True)
            c = make_ch(p, font_style="Bold")
            content(c, proj["name"])
            c2 = make_ch(p); br(c2)
            paras.append(p)

            for b in proj["bullets"]:
                p = para("regular",
                         left_indent=HANG_LEFT,
                         first_indent=HANG_FIRST,
                         space_after=bullet_space)
                c = make_ch(p)
                content(c, f"• {b}"); br(c)
                paras.append(p)
            paras.append(blank())
        paras.append(blank())

    return build_story(self_id, paras)


def xml_work_dstrict(self_id, me, job):
    """p2: d'strict 만 (heading 포함)"""
    bullet_space = job.get("layout", {}).get("bullet_space_after", "2")
    exps = job["work_commercial"][:1]   # index 0 = d'strict
    return build_story(self_id, _work_exp_paras(exps, bullet_space, include_heading=True))


def xml_work_plinqer(self_id, me, job):
    """p3: Plinqer 만 (heading 없음 — 템플릿 u278 heading 프레임 유지)"""
    bullet_space = job.get("layout", {}).get("bullet_space_after", "2")
    exps = job["work_commercial"][1:]   # index 1 = Plinqer
    return build_story(self_id, _work_exp_paras(exps, bullet_space, include_heading=False))


def xml_work_independent(self_id, me, job):
    paras = []
    p = para("heading"); c = make_ch(p)
    content(c, "Independent Practice"); br(c)
    paras.append(p)
    paras.append(blank())

    for w in job["work_independent"]:
        p = para("bold", keep_with_next=True); c = make_ch(p)
        content(c, w["name"]); br(c)
        paras.append(p)

        if w.get("subtitle"):
            p = para("bold"); c = make_ch(p)
            content(c, w["subtitle"]); br(c)
            paras.append(p)

        p = para("regular"); c = make_ch(p)
        br(c); content(c, w["body"].strip()); br(c); br(c)
        if w.get("support"):
            content(c, w["support"]); br(c)
        paras.append(p)

        if w.get("exhibited"):
            p = para("italic"); c = make_ch(p)
            content(c, f"Exhibited: {w['exhibited']}"); br(c)
            paras.append(p)

        paras.append(blank())
        paras.append(blank())

    return build_story(self_id, paras)


def xml_grants(self_id, me, job):
    paras = []
    p = para("heading"); c = make_ch(p)
    content(c, "Grants & Recognition"); br(c)
    paras.append(p)
    p = para("regular"); c = make_ch(p)
    br(c); br(c)
    for g in me["grants"]:
        content(c, g["body"]); br(c); br(c)
    paras.append(p)
    return build_story(self_id, paras)


def xml_exhibitions(self_id, me, job):
    paras = []
    p = para("heading"); c = make_ch(p)
    content(c, "Selected Exhibitions"); br(c)
    paras.append(p)
    p = para("regular"); c = make_ch(p)
    br(c); br(c)
    for ex in (job.get("exhibitions") or me["exhibitions"]):
        title = f" · {ex['title']}" if ex.get("title") else ""
        content(c, f"{ex['venue']}{title}  ·  {ex['year']}"); br(c)
    paras.append(p)
    return build_story(self_id, paras)


def xml_references(self_id, me, job):
    paras = []
    p = para("heading"); c = make_ch(p)
    content(c, "References"); br(c)
    paras.append(p)
    paras.append(blank())

    references = me.get("references", [])
    references_note = me.get("references_note", "References available upon request.")

    if not references:
        p = para("regular"); c = make_ch(p)
        content(c, references_note); br(c)
        paras.append(p)
        return build_story(self_id, paras)

    for i, r in enumerate(references):
        p = para("bold"); c = make_ch(p)
        content(c, r["name"]); br(c)
        paras.append(p)

        p = para("italic"); c = make_ch(p)
        content(c, r["title"]); br(c)
        paras.append(p)

        p = para("regular"); c = make_ch(p)
        content(c, f"{r['email']} · {r['phone']}"); br(c)
        paras.append(p)

        if r.get("pdf_url"):
            p = para("regular"); c = make_ch(p)
            content(c, "Reference Letter:"); br(c)
            make_hyperlink(p,
                           f"hl_ref_{i}_{self_id}",
                           r["pdf_url"], r["pdf_url"])
            c2 = make_ch(p); br(c2); br(c2)
            paras.append(p)
        else:
            p = para("regular"); c = make_ch(p); br(c); br(c)
            paras.append(p)

    return build_story(self_id, paras)


def xml_cl_date(self_id, me, job):
    # 실행 시점 날짜 자동 사용 (예: "4 Jun, 2026")
    from datetime import date as _date
    today = _date.today()
    date_str = f"{today.day} {today.strftime('%B')} {today.year}"
    p = para("italic")
    c = make_ch(p); content(c, date_str)
    return build_story(self_id, [p])


def xml_cl_recipient(self_id, me, job):
    j = job["job"]
    lines = []
    hm = j.get("hiring_manager", "").strip()
    if hm:                  lines.append(hm)
    if j.get("department"): lines.append(j["department"])
    lines.append(j["company"])
    lines.append(j["address"]["street"])
    lines.append(j["address"]["city"])
    p = para("bold"); c = make_ch(p)
    for i, line in enumerate(lines):
        content(c, line)
        if i < len(lines) - 1: br(c)
    return build_story(self_id, [p])


def xml_cl_salutation(self_id, me, job):
    hm = job["job"].get("hiring_manager", "").strip()
    text = f"Dear {hm}," if hm else "Dear Hiring Team,"
    p = para("regular"); c = make_ch(p); content(c, text)
    return build_story(self_id, [p])


def xml_cl_body(self_id, me, job):
    cl = job["coverletter"]
    parts = [cl["opening"].strip(), cl["pitch"].strip()]
    if cl.get("gap_note", "").strip():
        parts.append(cl["gap_note"].strip())
    parts.append(cl["closing"].strip())
    paras = []
    for i, part in enumerate(parts):
        p = para("regular"); c = make_ch(p); content(c, part)
        if i < len(parts) - 1:
            br(c); br(c)
        paras.append(p)
    return build_story(self_id, paras)


def xml_cl_signoff(self_id, me, job):
    """cl_signoff 프레임: 'Yours sincerely,' + 서명 (이미지 or 텍스트)
    me.toml [personal] signature_image = "경로" 로 이미지 서명 지정.
    """
    pi = me["personal"]
    sig_path = pi.get("signature_image", "").strip()

    # "Yours sincerely,"
    p1 = para("regular"); c1 = make_ch(p1)
    content(c1, "Yours sincerely,")
    paras = [p1, blank()]

    if sig_path:
        # 이미지 서명
        from pathlib import Path as _Path
        resolved = (_Path(__file__).parent / sig_path).resolve()
        uri = resolved.as_uri()
        p = para("signature"); cr = make_ch(p)
        rect = etree.SubElement(cr, "Rectangle",
            Self=f"sig_rect_{self_id}",
            ContentType="GraphicType",
            StrokeColor="Swatch/None",
            FillColor="Swatch/None",
            GeometricBounds="0 0 36 120")
        etree.SubElement(rect, "AnchoredObjectSetting",
            AnchoredPosition="InlinePosition",
            SpineRelative="false",
            LockPosition="false",
            PinPosition="false")
        img = etree.SubElement(rect, "Image", Self=f"sig_image_{self_id}")
        etree.SubElement(img, "Link",
            Self=f"sig_link_{self_id}",
            LinkResourceURI=uri,
            LinkClassID="35906",
            LinkClientID="257",
            LinkResourceFormat="$ID/PNG",
            StoredState="Normal",
            LinkStatus="Normal")
        paras.append(p)
    else:
        # 텍스트 서명 폴백 — "Yours sincerely,"와 동일한 regular 폰트
        p = para("regular"); c = make_ch(p)
        content(c, pi["name"])
        paras.append(p)

    return build_story(self_id, paras)


# ── Story ID 자동 감지 ────────────────────────────

def get_story_ids(template_path):
    """labels: ObjectExportOption CustomAltText → storyID
    unlabeled_list: 텍스트 키 → [storyID, ...] (페이지 순서 보장, 중복 허용)
    unlabeled: 텍스트 키 → storyID (첫 번째 매치만, 하위 호환)
    """
    labels = {}
    unlabeled_list = {}   # text_key → [sid, sid, ...]
    unlabeled = {}        # text_key → sid (첫 번째)

    with zipfile.ZipFile(template_path) as z:
        # 라벨 수집
        for name in sorted(z.namelist()):   # 페이지 순 정렬
            if not name.startswith("Spreads/"): continue
            tree = etree.fromstring(z.read(name))
            for elem in tree.iter():
                tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                if tag == "ObjectExportOption":
                    alt = elem.get("CustomAltText", "")
                    if alt and alt != "$ID/":
                        parent = elem.getparent()
                        sid = parent.get("ParentStory", "") if parent is not None else ""
                        labels[alt] = sid

        labeled_ids = set(labels.values())

        # 페이지 순으로 story 텍스트 수집 (Spread → ParentStory 순)
        seen_sids = set()
        for name in sorted(z.namelist()):
            if not name.startswith("Spreads/"): continue
            spread_tree = etree.fromstring(z.read(name))
            for tf in spread_tree.findall(".//{*}TextFrame"):
                sid = tf.get("ParentStory", "")
                if not sid or sid in labeled_ids or sid in seen_sids:
                    continue
                seen_sids.add(sid)
                story_path = f"Stories/Story_{sid}.xml"
                if story_path not in z.namelist(): continue
                story_tree = etree.fromstring(z.read(story_path))
                texts = [c.text for c in story_tree.findall(".//Content") if c.text]
                if texts:
                    key = texts[0].strip()[:20]
                    unlabeled_list.setdefault(key, []).append(sid)
                    if key not in unlabeled:   # 첫 번째만 저장
                        unlabeled[key] = sid

    return labels, unlabeled, unlabeled_list


# ── 메인 ─────────────────────────────────────────

def main():
    print(f"\n📄 CV 주입 시작 — {INPUT.name}\n")

    _hyperlinks.clear()
    _hl_counter[0] = 10

    with open(ME, "rb") as f:
        me = tomllib.load(f)
    with open(INPUT, "rb") as f:
        job = tomllib.load(f)

    # 출력 파일명: <이름>_<입력파일명>.idml
    my_name    = me["personal"]["name"].replace(" ", "")
    input_stem = INPUT.stem
    out_name   = f"{my_name}_{input_stem}.idml"

    labels, unlabeled, unlabeled_list = get_story_ids(TEMPLATE_IDML)

    def sid(label):
        return labels.get(label, "")

    def usid(keyword):
        """페이지 순 첫 번째 매치"""
        for k, v in unlabeled.items():
            if keyword.lower() in k.lower():
                return v
        return ""

    def usid_nth(keyword, n=0):
        """페이지 순 n번째 매치 (0=첫 번째)"""
        for k, lst in unlabeled_list.items():
            if keyword.lower() in k.lower():
                return lst[n] if n < len(lst) else ""
        return ""

    story_builders = {
        # p1
        sid("cv_initials"):    lambda s: xml_initials(        s, me, job),
        sid("cv_name_p1"):     lambda s: xml_name_p1(         s, me, job),
        sid("cv_about"):       lambda s: xml_about(           s, me, job),
        sid("cv_contact_p1"):  lambda s: xml_contact(         s, me, job),
        # p2
        sid("cv_initials_p2"): lambda s: xml_initials(        s, me, job),
        sid("cv_name_p2"):     lambda s: xml_name_hdr(        s, me, job),
        usid("Skills"):        lambda s: xml_skills(          s, me, job),
        usid("Education"):     lambda s: xml_education(       s, me, job),
        # p2: d'strict 전용 (usid_nth 0번 = 페이지 순 첫 번째 "Work Experience")
        usid_nth("Work Experien", 0): lambda s: xml_work_dstrict(     s, me, job),
        # p3: Plinqer 전용 ("Technical Artist"로 시작하는 신규 분리 프레임)
        usid("Technical Art"):        lambda s: xml_work_plinqer(     s, me, job),
        # p3
        sid("cv_initials_p3"): lambda s: xml_initials(        s, me, job),
        sid("cv_name_p3"):     lambda s: xml_name_hdr(        s, me, job),
        sid("cv_languages"):   lambda s: xml_languages(       s, me, job),
        usid("Independent"):   lambda s: xml_work_independent(s, me, job),
        usid("Grants"):        lambda s: xml_grants(          s, me, job),
        sid("cv_exhibitions"): lambda s: xml_exhibitions(     s, me, job),
        usid("References"):    lambda s: xml_references(      s, me, job),
        # p4
        sid("cl_initials"):    lambda s: xml_initials(        s, me, job),
        sid("cl_name"):        lambda s: xml_name_hdr(        s, me, job),
        sid("cl_date"):        lambda s: xml_cl_date(         s, me, job),
        sid("cl_recipient"):   lambda s: xml_cl_recipient(    s, me, job),
        sid("cl_salutation"):  lambda s: xml_cl_salutation(   s, me, job),
        sid("cl_body"):        lambda s: xml_cl_body(         s, me, job),
        # cl_signoff 레이블(u425)이 sid()로 처리 — usid 불필요
        sid("cl_signoff"):     lambda s: xml_cl_signoff(     s, me, job),
        sid("cl_contact"):     lambda s: xml_contact(         s, me, job),
    }

    tmp = OUTPUT_DIR / "_tmp"
    if tmp.exists(): shutil.rmtree(tmp)
    tmp.mkdir()

    with zipfile.ZipFile(TEMPLATE_IDML, "r") as z:
        z.extractall(tmp)

    stories_dir = tmp / "Stories"
    for story_id, builder in story_builders.items():
        if not story_id: continue
        story_file = stories_dir / f"Story_{story_id}.xml"
        if story_file.exists():
            with open(story_file, "wb") as f:
                f.write(builder(story_id))
            print(f"  ✓ Story_{story_id}")
        else:
            print(f"  ⚠ Story_{story_id} 없음")

    patch_designmap(tmp, _hyperlinks)

    out_idml = OUTPUT_DIR / out_name
    out_tmp  = OUTPUT_DIR / (out_name + ".tmp")
    with zipfile.ZipFile(out_tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        # mimetype: 반드시 첫 항목, 비압축
        mi = zipfile.ZipInfo("mimetype")
        mi.compress_type = zipfile.ZIP_STORED
        zout.writestr(mi, "application/vnd.adobe.indesign-idml-package")
        for f in sorted(tmp.rglob("*")):
            if f.is_file() and f.name != "mimetype":
                zout.write(f, f.relative_to(tmp).as_posix())  # Windows 경로 슬래시 통일
    out_tmp.replace(out_idml)

    shutil.rmtree(tmp)
    print(f"\n  ✅ 완료: output/{out_name}")
    print(f"  → InDesign에서 열어 확인 후 PDF export\n")


if __name__ == "__main__":
    main()