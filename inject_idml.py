#!/usr/bin/env python3
"""
IDML 주입 스크립트 v9
me.toml (고정) + <Company>_<Position>.toml (회사별) → output/<Name>_<Company>_<Position>.idml

사용법:
  python inject_idml.py                          # 폴더에 toml이 1개일 때 자동 선택
  python inject_idml.py PrincipleHR_RealtimeVFXArtist   # 특정 toml 지정 (.toml 생략 가능)
"""

import sys
import re
import math
import zipfile
import shutil
import tomllib
from pathlib import Path
from lxml import etree

# Windows 콘솔(cp949)에서 이모지 출력 시 UnicodeEncodeError 방지 — 출력 인코딩을 UTF-8로 강제
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def _load_toml(path):
    """TOML 로드 — 채팅에서 복사·붙여넣기할 때 따라오는 마크다운 코드펜스
    (맨 앞 ```toml / 맨 끝 ```)를 자동으로 떼어내고 파싱한다."""
    lines = path.read_text(encoding="utf-8").splitlines()
    while lines and lines[0].lstrip().startswith("```"):
        lines.pop(0)
    while lines and lines[-1].lstrip().startswith("```"):
        lines.pop()
    return tomllib.loads("\n".join(lines))


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

# 내용 길이에 따라 '높이만' 아래로(수직) 자동 확장할 프레임(라벨) — 텍스트 넘침(overset) 방지.
# 커버레터 수신인 블록(job + job.address)이 길어지면 위는 고정, 아래로 늘어남.
AUTOSIZE_DOWN_LABELS = ["cl_recipient"]

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


# 본문 텍스트 안의 URL을 자동으로 하이퍼링크로 변환 (커버레터 등)
_URL_RE      = re.compile(r'https?://\S+')
_TRAIL_PUNCT = set('.,;:!?)]}>')


def render_text_with_links(parent, text, link_prefix):
    """text 안의 http(s) URL을 찾아 하이퍼링크로, 나머지는 일반 텍스트로 렌더한다.
    parent(ParagraphStyleRange) 아래에 CharacterStyleRange들을 순서대로 추가하고,
    마지막으로 만든 CharacterStyleRange를 돌려준다(Br 부착용)."""
    last_end, seg, last_c = 0, 0, None
    for m in _URL_RE.finditer(text):
        if m.start() > last_end:                     # URL 앞의 일반 텍스트
            last_c = make_ch(parent)
            content(last_c, text[last_end:m.start()])
        url, trail = m.group(0), ""
        while url and url[-1] in _TRAIL_PUNCT:        # 끝의 문장부호는 링크에서 제외
            trail = url[-1] + trail
            url = url[:-1]
        make_hyperlink(parent, f"{link_prefix}_{seg}", url, url)
        seg += 1
        if trail:
            last_c = make_ch(parent)
            content(last_c, trail)
        last_end = m.end()
    if last_end < len(text):                          # 마지막 남은 텍스트
        last_c = make_ch(parent)
        content(last_c, text[last_end:])
    if last_c is None:                                # 텍스트가 비었거나 전부 URL인 경우
        last_c = make_ch(parent)
        content(last_c, "")
    return last_c


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

        # 존재 판정은 순번 key가 아니라 URL 기반 dest_self 로 한다.
        # (순번 key 로 매칭하면 master.idml 에 남은 과거 빌드의 destination 과
        #  key 가 겹쳐, 텍스트는 새 URL 인데 클릭 destination 은 옛 URL 로 남는
        #  문제가 생긴다. URL 이 다르면 dest_self 가 다르므로 항상 새로 삽입된다.)
        existing_dest = next(
            (c for c in root
             if (c.tag.split('}')[-1] if '}' in c.tag else c.tag) == 'HyperlinkURLDestination'
             and c.get("Self") == dest_self),
            None
        )
        if existing_dest is None:
            dest_elem = etree.Element("HyperlinkURLDestination",
                                      Self=dest_self,
                                      DestinationUniqueKey=key,
                                      Name=url,
                                      DestinationURL=url,
                                      Hidden="true")
            root.insert(insert_idx, dest_elem)
            insert_idx += 1
        else:
            # 같은 URL 의 destination 이 이미 있으면, 옛 빌드 잔재일 수 있으니
            # URL 속성을 현재 값으로 강제 동기화한다(안전망).
            existing_dest.set("Name", url)
            existing_dest.set("DestinationURL", url)

        # Hyperlink 의 Self 도 순번 key 가 아니라 source_self 기반으로 잡아
        # master 잔재의 hl_{같은번호} 와 충돌하지 않게 한다.
        hl_self = f"hl_{source_self}"
        exists_hl = any(
            c.get("Self") == hl_self
            for c in root
            if (c.tag.split('}')[-1] if '}' in c.tag else c.tag) == 'Hyperlink'
        )
        if not exists_hl:
            hl = etree.Element("Hyperlink",
                               Self=hl_self,
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


# ── 텍스트 프레임 자동 높이(아래로 확장) ──────────

def _localname(elem):
    return elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag


def _estimate_text_height(lines, frame_width_pt, font_pt=10,
                          leading_factor=1.3, char_w_factor=0.6, pad_lines=1):
    """주어진 줄 목록이 frame_width_pt 폭에서 차지할 대략적인 높이(pt)를 추정.
    줄바꿈(wrap)을 넉넉히 잡아 과대 추정 — 남는 공간은 안 보이므로 넘침보다 안전.
    """
    leading = font_pt * leading_factor
    usable = max(1.0, frame_width_pt)
    total = 0
    for ln in lines:
        text = (ln or "").strip()
        if not text:
            total += 1                       # 빈 줄도 한 줄 높이 차지
            continue
        approx_w = len(text) * font_pt * char_w_factor
        total += max(1, math.ceil(approx_w / usable))
    return (total + pad_lines) * leading


def patch_textframe_autosize(tmp_dir, configs, reference_point="TopLeftPoint"):
    """configs: { story_id: {"fit_lines": [...], "font_pt": int} }
    대상 TextFrame을 '높이만 자동 조절(아래로)'로 설정하고,
    fit_lines가 있으면 그 내용에 맞춰 필요한 높이만큼 프레임을 아래로 늘린다.
    (InDesign이 IDML을 열 때 자동크기를 즉시 반영하지 않는 경우가 있어
     지오메트리 높이도 직접 키워 넘침을 확실히 막는다. 위 고정·아래로만 확장.)
    """
    targets = {sid: cfg for sid, cfg in configs.items() if sid}
    if not targets:
        return
    patched = grown = 0
    for spread_path in sorted((tmp_dir / "Spreads").glob("*.xml")):
        tree = etree.parse(str(spread_path))
        root = tree.getroot()
        changed = False
        for tf in root.findall(".//{*}TextFrame"):
            sid = tf.get("ParentStory")
            if sid not in targets:
                continue
            cfg = targets[sid]
            pref = next((c for c in tf if _localname(c) == "TextFramePreference"), None)
            if pref is None:
                pref = etree.Element("TextFramePreference")
                tf.insert(0, pref)
            pref.set("AutoSizingReferencePoint", reference_point)
            pref.set("AutoSizingType", "HeightOnly")
            pref.set("UseNoLineBreaksForAutoSizing", "false")

            fit_lines = cfg.get("fit_lines")
            if fit_lines:
                width  = float(pref.get("TextColumnFixedWidth") or 0) or 1.0
                need_h = _estimate_text_height(fit_lines, width, cfg.get("font_pt", 10))
                pts = tf.findall(".//{*}PathPointType")
                ys  = [float(pp.get("Anchor").split()[1]) for pp in pts]
                if ys:
                    top_y, bot_y = min(ys), max(ys)
                    new_bot = max(bot_y, top_y + need_h)   # 줄이지 않고, 필요할 때만 아래로 확장
                    if new_bot > bot_y + 0.5:
                        for pp in pts:
                            for attr in ("Anchor", "LeftDirection", "RightDirection"):
                                xv, yv = pp.get(attr).split()
                                if abs(float(yv) - bot_y) < 0.5:   # 아래쪽 모서리 점만 이동
                                    pp.set(attr, f"{xv} {new_bot}")
                        grown += 1
            changed = True
            patched += 1
        if changed:
            tree.write(str(spread_path), xml_declaration=True,
                       encoding="UTF-8", pretty_print=True)
    print(f"  ✓ 자동 높이 설정 {patched}개 (그 중 프레임 직접 확장 {grown}개)")


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


# ── smallTitle 라벨: 자동 생성 + me.toml 자동 등록 ──

# key를 사람이 읽는 라벨로 변환할 때 쓰는 특수 약어 (대문자 유지)
_LABEL_SPECIALS = {
    "ai": "AI", "vr": "VR", "ar": "AR", "xr": "XR", "ml": "ML",
    "ui": "UI", "ux": "UX", "2d": "2D", "3d": "3D", "vfx": "VFX",
}


def humanize_key(key):
    """'ai_tools' → 'AI Tools', 'vr_ar' → 'VR / AR' 같은 기본 라벨 자동 생성."""
    words = key.replace("-", "_").split("_")
    parts = []
    for w in words:
        if not w:
            continue
        parts.append(_LABEL_SPECIALS[w.lower()] if w.lower() in _LABEL_SPECIALS
                     else w.capitalize())
    return " ".join(parts)


def _insert_into_section(text, section, key, value):
    """me.toml 텍스트의 [section] 블록 끝에 `key = "value"` 한 줄 삽입.
    섹션이 없으면 파일 끝에 새 섹션을 만들어 추가. (주석/서식 보존)"""
    lines = text.splitlines()
    new_line = f'{key} = "{value}"'

    header_idx = next(
        (i for i, ln in enumerate(lines) if ln.strip() == f"[{section}]"), None)
    if header_idx is None:
        return text.rstrip("\n") + f"\n\n[{section}]\n{new_line}\n"

    # 섹션 끝 = 다음 '[' 로 시작하는 줄 직전 (없으면 파일 끝)
    end_idx = next(
        (j for j in range(header_idx + 1, len(lines))
         if lines[j].lstrip().startswith("[")), len(lines))
    # 섹션 끝의 빈 줄들 앞에 끼워넣기
    while end_idx - 1 > header_idx and lines[end_idx - 1].strip() == "":
        end_idx -= 1
    lines.insert(end_idx, new_line)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def sync_skill_pool(me, job, me_path):
    """job의 skills_order에 쓰인 key 중 me.toml에 없는 것을 자동 등록.
       - [skills_labels] : 자동 생성한 smallTitle 라벨
       - [skills]        : 빈 내용 슬롯 (사용자가 나중에 채움)
    """
    order = job.get("skills_order", {}).get("list", [])
    labels = me.setdefault("skills_labels", {})
    skills = me.setdefault("skills", {})

    added_labels, added_slots = [], []
    text = me_path.read_text(encoding="utf-8")

    for key in order:
        if key not in labels:
            label = humanize_key(key)
            text = _insert_into_section(text, "skills_labels", key, label)
            labels[key] = label
            added_labels.append((key, label))
        if key not in skills:
            text = _insert_into_section(text, "skills", key, "")
            skills[key] = ""
            added_slots.append(key)

    if added_labels or added_slots:
        me_path.write_text(text, encoding="utf-8")
        if added_labels:
            print("  ➕ me.toml [skills_labels]에 새 smallTitle 자동 추가:")
            for key, label in added_labels:
                print(f'       {key} = "{label}"')
        if added_slots:
            print(f"  ➕ me.toml [skills]에 빈 슬롯 추가 (내용 채워주세요): {added_slots}")
    return me


def xml_skills(self_id, me, job):
    order = job["skills_order"]["list"]
    # 라벨/내용 모두 job(inject) 우선, 없으면 me.toml 폴백.
    # 이렇게 하면 me.toml(상수)을 건드리지 않고 포지션 전용 inject 에서만
    # Skills 를 덮어쓸 수 있다.
    me_labels  = me.get("skills_labels", {})
    job_labels = job.get("skills_labels", {})
    me_skills  = me.get("skills", {})
    job_skills = job.get("skills", {})
    paras = []
    p = para("heading"); c = make_ch(p)
    content(c, "Skills"); br(c)
    paras.append(p)
    paras.append(blank())

    for key in order:
        label = job_labels.get(key) or me_labels.get(key) or humanize_key(key)
        content_str = (job_skills.get(key) or me_skills.get(key, "")).strip()
        # 내용이 비어 있으면 (자동 슬롯 등) 렌더 생략 — CV에 빈 제목 안 남김
        if not content_str:
            print(f"  ⚠ skills: '{key}' 내용이 비어 있음 — me.toml [skills]에 채워주세요. 건너뜀")
            continue
        items = [i.strip() for i in content_str.split("·") if i.strip()]
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


# ── Work Experience / Independent Practice (페이지당 프레임 1개) ──
#
# 각 경력 항목에 page 번호를 달아 어느 페이지 프레임에 들어갈지 정함:
#   [[work_commercial]]  page = 2   (생략 시 기본 2)
#   [[work_independent]] page = 3   (생략 시 기본 3)
# 섹션 제목은 job [layout] 에서 바꿀 수 있음:
#   work_title         (기본 "Work Experience")
#   independent_title  (기본 "Independent Practice")
# 한 페이지 프레임은 commercial 그룹 → independent 그룹 순으로 채워지며,
# 각 그룹 앞에 제목(heading)이 붙음. heading 스타일에 밑줄(Paragraph Rule)이
# 들어 있어 줄(line)이 자동으로 그려짐.

def _heading_para(title):
    p = para("heading"); c = make_ch(p)
    content(c, title); br(c)
    return p


def _render_commercial_entry(paras, exp, bullet_space):
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

        # bullets(목록) / detail(문단) 둘 다 지원
        if proj.get("bullets"):
            for b in proj["bullets"]:
                p = para("regular",
                         left_indent=HANG_LEFT,
                         first_indent=HANG_FIRST,
                         space_after=bullet_space)
                c = make_ch(p)
                content(c, f"• {b}"); br(c)
                paras.append(p)
        elif proj.get("detail"):
            p = para("regular", space_after=bullet_space)
            c = make_ch(p)
            content(c, proj["detail"].strip()); br(c)
            paras.append(p)
        paras.append(blank())
    paras.append(blank())


def _render_independent_entry(paras, w):
    # 제목: 신규 title / 구 name
    title = w.get("title") or w.get("name", "")
    p = para("bold", keep_with_next=True); c = make_ch(p)
    content(c, title); br(c)
    paras.append(p)

    # 신규: period · tech 한 줄 / 구: subtitle
    meta_bits = [b for b in (w.get("period"), w.get("tech")) if b]
    if meta_bits:
        p = para("italic"); c = make_ch(p)
        content(c, "  ·  ".join(meta_bits)); br(c)
        paras.append(p)
    elif w.get("subtitle"):
        p = para("bold"); c = make_ch(p)
        content(c, w["subtitle"]); br(c)
        paras.append(p)

    # 본문: 신규 detail / 구 body
    body = (w.get("detail") or w.get("body") or "").strip()
    p = para("regular"); c = make_ch(p)
    br(c); content(c, body); br(c); br(c)
    if w.get("support"):
        content(c, w["support"]); br(c)
    paras.append(p)

    if w.get("exhibited"):
        p = para("italic"); c = make_ch(p)
        content(c, f"Exhibited: {w['exhibited']}"); br(c)
        paras.append(p)

    paras.append(blank())
    paras.append(blank())


def xml_experience_page(self_id, me, job, page):
    """페이지 1개 = 경력 텍스트 프레임 1개.
    이 page 번호가 달린 항목만 모아 commercial → independent 순으로 렌더.
    각 그룹이 비어 있지 않으면 그 앞에 제목(밑줄 포함)을 붙임.
    """
    layout       = job.get("layout", {})
    work_title   = layout.get("work_title", "Work Experience")
    indep_title  = layout.get("independent_title", "Independent Practice")
    bullet_space = layout.get("bullet_space_after", "2")

    commercial  = [e for e in job.get("work_commercial", [])  if e.get("page", 2) == page]
    independent = [w for w in job.get("work_independent", []) if w.get("page", 3) == page]

    paras = []
    if commercial:
        paras.append(_heading_para(work_title))
        paras.append(blank())
        for exp in commercial:
            _render_commercial_entry(paras, exp, bullet_space)
    if independent:
        paras.append(_heading_para(indep_title))
        paras.append(blank())
        for w in independent:
            _render_independent_entry(paras, w)

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


def _recipient_lines(job):
    """커버레터 수신인 블록(job + job.address)이 렌더링하는 줄 목록.
    xml_cl_recipient 와 높이 추정이 동일한 내용을 보도록 한 곳에서 생성."""
    j = job["job"]
    lines = []
    hm = j.get("hiring_manager", "").strip()
    if hm:                  lines.append(hm)
    if j.get("department"): lines.append(j["department"])
    lines.append(j["company"])
    lines.append(j["address"]["street"])
    lines.append(j["address"]["city"])
    return lines


def xml_cl_recipient(self_id, me, job):
    lines = _recipient_lines(job)
    p = para("bold"); c = make_ch(p)
    for i, line in enumerate(lines):
        content(c, line)
        if i < len(lines) - 1: br(c)
    return build_story(self_id, [p])


def xml_cl_salutation(self_id, me, job):
    # 신규: cover_letter.salutation 우선 / 구: job.hiring_manager 기반 자동 생성
    cl = job.get("cover_letter") or job.get("coverletter") or {}
    text = (cl.get("salutation") or "").strip()
    if not text:
        hm = job["job"].get("hiring_manager", "").strip()
        text = f"Dear {hm}," if hm else "Dear Hiring Team,"
    p = para("regular"); c = make_ch(p); content(c, text)
    return build_story(self_id, [p])


def xml_cl_body(self_id, me, job):
    cl = job.get("cover_letter") or job.get("coverletter") or {}
    # 두 가지 형식 지원:
    #   body = "여러 문단" (빈 줄로 구분) → 신규
    #   opening / pitch / gap_note / closing → 구
    if cl.get("body"):
        raw = [cl["body"]]
    else:
        raw = [cl.get("opening", ""), cl.get("pitch", "")]
        if cl.get("gap_note", "").strip():
            raw.append(cl["gap_note"])
        raw.append(cl.get("closing", ""))
    # 빈 줄(\n\n) 기준으로 문단 분리 — closing 안의 포트폴리오 줄도 별도 문단이 됨
    parts = [sub.strip() for block in raw for sub in block.split("\n\n") if sub.strip()]

    paras = []
    for i, part in enumerate(parts):
        p = para("regular")
        # 문단 안의 URL은 자동으로 하이퍼링크 처리 (커버레터 링크 클릭 가능)
        render_text_with_links(p, part, link_prefix=f"hl_cl_{self_id}_{i}")
        if i < len(parts) - 1:
            c2 = make_ch(p); br(c2); br(c2)
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


# ── 커버레터 존재 여부 확인 + 페이지 삭제 ─────────

def has_cover_letter(job):
    """job TOML에 커버레터 내용이 실제로 있는지 확인."""
    cl = job.get("cover_letter") or job.get("coverletter") or {}
    if not cl:
        return False
    has_body   = bool(cl.get("body", "").strip())
    has_legacy = any(bool(cl.get(k, "").strip()) for k in ("opening", "pitch", "closing"))
    return has_body or has_legacy


def delete_cl_page(tmp_dir, cl_story_ids):
    """커버레터 Spread를 tmp 폴더에서 삭제하고 designmap.xml을 업데이트한다.
    삭제된 story ID 집합을 반환 (호출자가 story_builders 루프를 건너뛰는 데 사용)."""
    cl_ids = {sid for sid in cl_story_ids if sid}
    if not cl_ids:
        print("  ⚠ 커버레터 story ID 없음 — 페이지 삭제 건너뜀")
        return set()

    # 커버레터 story ID를 포함한 Spread 파일 탐색
    spreads_dir = tmp_dir / "Spreads"
    target_spread = None
    spread_story_ids = set()
    for spread_path in sorted(spreads_dir.glob("*.xml")):
        tree = etree.parse(str(spread_path))
        ids_in_spread = {
            tf.get("ParentStory")
            for tf in tree.findall(".//{*}TextFrame")
            if tf.get("ParentStory")
        }
        if ids_in_spread & cl_ids:
            target_spread = spread_path
            spread_story_ids = ids_in_spread
            break

    if not target_spread:
        print("  ⚠ 커버레터 Spread를 찾지 못함 — 페이지 삭제 건너뜀")
        return set()

    # designmap.xml에서 해당 Spread 참조 제거
    dm_path = tmp_dir / "designmap.xml"
    dm_tree = etree.parse(str(dm_path))
    dm_root = dm_tree.getroot()
    for elem in list(dm_root):
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "Spread" and target_spread.name in elem.get("src", ""):
            dm_root.remove(elem)
            break
    dm_tree.write(str(dm_path), xml_declaration=True, encoding="UTF-8", pretty_print=True)

    # Spread 파일 삭제
    target_spread.unlink()

    # 해당 Spread에 속한 Story 파일도 함께 삭제 (orphan 방지)
    stories_dir = tmp_dir / "Stories"
    for sid in spread_story_ids:
        sf = stories_dir / f"Story_{sid}.xml"
        if sf.exists():
            sf.unlink()

    print(f"  🗑 커버레터 페이지 삭제: {target_spread.name} "
          f"(story {len(spread_story_ids)}개)")
    return spread_story_ids


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

    me  = _load_toml(ME)
    job = _load_toml(INPUT)

    # job에서 쓰인 새 skill key를 me.toml [skills_labels] 풀에 자동 등록
    sync_skill_pool(me, job, ME)

    # 출력 파일명: <이름>_<입력파일명>.idml
    my_name    = me["personal"]["name"].replace(" ", "")
    input_stem = INPUT.stem
    # 출력 파일명에 'CV' 포함 — 예: JooyoungGwak_CV_1956Individuals_TechnicalArtist.idml
    out_name   = f"{my_name}_CV_{input_stem}.idml"

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

    # 경력 프레임: 라벨(exp_p2/exp_p3) 우선, 없으면 페이지 순 "Work Experience" 프레임으로 폴백
    # → 템플릿에 라벨을 아직 안 붙였어도 동작함.
    p2_exp = sid("exp_p2") or usid_nth("Work Experien", 0)
    p3_exp = sid("exp_p3") or usid_nth("Work Experien", 1)

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
        # p2 경력 프레임 (page=2 항목)
        p2_exp:                lambda s: xml_experience_page(  s, me, job, 2),
        # p3
        sid("cv_initials_p3"): lambda s: xml_initials(        s, me, job),
        sid("cv_name_p3"):     lambda s: xml_name_hdr(        s, me, job),
        sid("cv_languages"):   lambda s: xml_languages(       s, me, job),
        # p3 경력 프레임 (page=3 항목: 이어지는 work experience + independent practice)
        p3_exp:                lambda s: xml_experience_page(  s, me, job, 3),
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

    # 커버레터 내용이 없으면 cl 페이지 전체를 삭제
    _cl_label_keys = ["cl_initials", "cl_name", "cl_date", "cl_recipient",
                      "cl_salutation", "cl_body", "cl_signoff", "cl_contact"]
    deleted_story_ids: set = set()
    if not has_cover_letter(job):
        print("  ℹ 커버레터 내용 없음 — 커버레터 페이지를 삭제합니다")
        _cl_sids = {labels.get(k) for k in _cl_label_keys if labels.get(k)}
        deleted_story_ids = delete_cl_page(tmp, _cl_sids)

    stories_dir = tmp / "Stories"
    for story_id, builder in story_builders.items():
        if not story_id: continue
        if story_id in deleted_story_ids: continue   # cl 페이지 삭제됨 — 건너뜀
        story_file = stories_dir / f"Story_{story_id}.xml"
        if story_file.exists():
            with open(story_file, "wb") as f:
                f.write(builder(story_id))
            print(f"  ✓ Story_{story_id}")
        else:
            print(f"  ⚠ Story_{story_id} 없음")

    patch_designmap(tmp, _hyperlinks)

    # 지정 프레임은 자동크기 ON, 수신인 프레임은 내용 길이에 맞춰 아래로 직접 확장
    # (커버레터 페이지가 삭제된 경우 해당 프레임은 건너뜀)
    autosize_cfg = {
        labels.get(lbl): {}
        for lbl in AUTOSIZE_DOWN_LABELS
        if labels.get(lbl) and labels.get(lbl) not in deleted_story_ids
    }
    rec_sid = labels.get("cl_recipient")
    if rec_sid and rec_sid not in deleted_story_ids:
        autosize_cfg[rec_sid] = {"fit_lines": _recipient_lines(job), "font_pt": 10}
    patch_textframe_autosize(tmp, autosize_cfg)

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