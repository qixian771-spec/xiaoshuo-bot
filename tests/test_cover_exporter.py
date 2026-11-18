from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile

from PIL import Image

from xiaoshuo_bot.cover import (
    COVER_HEIGHT,
    COVER_WIDTH,
    DEFAULT_FONTS_DIR,
    MAX_FILE_SIZE_BYTES,
    MIN_FILE_SIZE_BYTES,
    SAFE_MARGIN_PX,
    CoverGenerationError,
    FontAssetMissingError,
    ImageProviderNotConfiguredError,
    generate_cover,
    generate_cover_for_project,
    load_bundled_font,
    validate_bundled_font_assets,
)
from xiaoshuo_bot.exporter import (
    ExportValidationError,
    convert_to_corner_quotes,
    export_qimao_bundle,
    export_tomato_bundle,
    export_zhihu_yanxuan_bundle,
    strip_chapter_title_prefix,
)
from xiaoshuo_bot.llm import RoleEndpointConfig, RoleModelRouter
from xiaoshuo_bot.models import ChapterArtifact, CharacterAnchor, ProjectMeta
from xiaoshuo_bot.sanitizers import InvalidTweetAliasError
from xiaoshuo_bot.transcreator import (
    GlossaryValidationError,
    TranscreatedChapter,
    TranscreationGlossary,
    TranscreationProviderError,
    WebnovelTranscreator,
    build_glossary,
    convert_cny_to_usd_purchasing_power,
    export_english_serialization_bundle,
    normalize_localization_text,
    validate_english_chapter_text,
)


def _make_sample_webp_bytes(width: int = 768, height: int = 1024) -> bytes:
    img = Image.new("RGB", (width, height), (22, 34, 58))
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=90)
    return buf.getvalue()


class _FakeCPAServer:
    """In-process 127.0.0.1:0 HTTP server simulating CPA Gemini image & OpenAI SSE endpoints."""

    def __init__(self, *, image_status: int = 200, sse_content: str = "") -> None:
        self.image_status = image_status
        self.sse_content = sse_content
        self.recorded_paths: list[str] = []
        self.recorded_prompts: list[str] = []
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length).decode("utf-8", errors="replace")
                payload = json.loads(raw_body) if raw_body else {}
                parent.recorded_paths.append(self.path)

                if ":generateContent" in self.path:
                    parts = (payload.get("contents") or [{}])[0].get("parts") or [{}]
                    parent.recorded_prompts.append(str(parts[0].get("text", "")))
                    if parent.image_status != 200:
                        err = json.dumps({"error": {"message": "upstream image failure"}}).encode("utf-8")
                        self.send_response(parent.image_status)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(err)))
                        self.end_headers()
                        self.wfile.write(err)
                        return

                    webp_b64 = base64.b64encode(_make_sample_webp_bytes()).decode("ascii")
                    resp_obj = {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {"text": "clean cover illustration"},
                                        {"inlineData": {"mimeType": "image/webp", "data": webp_b64}},
                                    ]
                                }
                            }
                        ],
                        "usageMetadata": {"promptTokenCount": 32, "candidatesTokenCount": 128},
                    }
                    body = json.dumps(resp_obj).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if self.path.endswith("/chat/completions"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.end_headers()
                    chunks = [
                        {
                            "id": "chatcmpl-1",
                            "model": "gpt-test",
                            "choices": [{"index": 0, "delta": {"content": parent.sse_content}, "finish_reason": None}],
                        },
                        {
                            "id": "chatcmpl-1",
                            "model": "gpt-test",
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 50, "completion_tokens": 90},
                        },
                    ]
                    for ch in chunks:
                        self.wfile.write(f"data: {json.dumps(ch, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    return

                self.send_response(404)
                self.end_headers()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_port
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "_FakeCPAServer":
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=3)


class TestCoverPipeline(unittest.TestCase):
    def test_bundled_ofl_font_valid_and_missing_raises_error(self) -> None:
        font_path, ofl_path = validate_bundled_font_assets()
        self.assertTrue(font_path.is_file())
        self.assertTrue(ofl_path.is_file())
        ofl_text = ofl_path.read_text(encoding="utf-8")
        self.assertIn("SIL OPEN FONT LICENSE", ofl_text.upper())
        self.assertIn("Version 1.1", ofl_text)

        font = load_bundled_font(44)
        bbox = font.getbbox("被裁当天我的工位能刷出百亿 著")
        self.assertGreater(bbox[2] - bbox[0], 100)
        self.assertGreater(bbox[3] - bbox[1], 20)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Missing directory
            with self.assertRaises(FontAssetMissingError):
                validate_bundled_font_assets(tmp_path / "nonexistent_fonts")

            # Missing OFL.txt
            no_ofl_dir = tmp_path / "no_ofl"
            no_ofl_dir.mkdir()
            (no_ofl_dir / font_path.name).write_bytes(font_path.read_bytes())
            with self.assertRaises(FontAssetMissingError):
                validate_bundled_font_assets(no_ofl_dir)

            # Corrupt font with valid OFL.txt
            bad_font_dir = tmp_path / "bad_font"
            bad_font_dir.mkdir()
            (bad_font_dir / "OFL.txt").write_text(ofl_text, encoding="utf-8")
            (bad_font_dir / "corrupt.ttf").write_bytes(b"not a real ttf file" * 100)
            with self.assertRaises(FontAssetMissingError):
                validate_bundled_font_assets(bad_font_dir)

    def test_ai_image_default_without_provider_raises_explicit_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "cover.png"
            with self.assertRaises(ImageProviderNotConfiguredError):
                generate_cover(
                    title="白雾渡口",
                    pen_name="林栖",
                    output_path=out,
                )

            router_no_image = RoleModelRouter(
                roles={
                    "writer": RoleEndpointConfig(
                        role="writer",
                        base_url="http://127.0.0.1:8317/v1",
                        model="gpt-test",
                        api_keys=["sk-test-1234"],
                    )
                }
            )
            with self.assertRaises(ImageProviderNotConfiguredError):
                generate_cover(
                    title="白雾渡口",
                    pen_name="林栖",
                    output_path=out,
                    base_mode="ai_image",
                    router=router_no_image,
                )

    def test_ai_image_mode_with_simulated_gemini_cpa_server(self) -> None:
        with _FakeCPAServer(image_status=200) as srv, tempfile.TemporaryDirectory() as tmp:
            router = RoleModelRouter(
                roles={
                    "image": RoleEndpointConfig(
                        role="image",
                        base_url=srv.base_url,
                        model="gemini-2.5-flash-image",
                        api_keys=["cpa-image-secret"],
                        provider_type="gemini_native",
                    )
                }
            )
            out_png = Path(tmp) / "ai_cover.png"
            res = generate_cover(
                title="被裁当天我的工位能刷出百亿",
                pen_name="青崖客",
                genre="都市脑洞",
                output_path=out_png,
                base_mode="ai_image",
                router=router,
                output_format="PNG",
            )
            self.assertEqual(res.width, COVER_WIDTH)
            self.assertEqual(res.height, COVER_HEIGHT)
            self.assertEqual(res.mode, "RGB")
            self.assertGreaterEqual(res.file_size_bytes, MIN_FILE_SIZE_BYTES)
            self.assertLessEqual(res.file_size_bytes, MAX_FILE_SIZE_BYTES)
            self.assertEqual(res.font_path.parent, DEFAULT_FONTS_DIR)
            self.assertTrue(any(":generateContent" in p for p in srv.recorded_paths))
            self.assertTrue(any("NO text" in pr for pr in srv.recorded_prompts))

            # Verify safe margin >= 42px on all 4 sides
            for bbox in (res.title_bbox, res.author_bbox):
                self.assertGreaterEqual(bbox[0], SAFE_MARGIN_PX)
                self.assertGreaterEqual(bbox[1], SAFE_MARGIN_PX)
                self.assertLessEqual(bbox[2], COVER_WIDTH - SAFE_MARGIN_PX)
                self.assertLessEqual(bbox[3], COVER_HEIGHT - SAFE_MARGIN_PX)

            with Image.open(out_png) as img:
                self.assertEqual(img.size, (600, 800))
                self.assertEqual(img.mode, "RGB")

        # Verify upstream failure raises CoverGenerationError (no silent procedural fallback)
        with _FakeCPAServer(image_status=500) as err_srv, tempfile.TemporaryDirectory() as tmp:
            router = RoleModelRouter(
                roles={
                    "image": RoleEndpointConfig(
                        role="image",
                        base_url=err_srv.base_url,
                        model="gemini-2.5-flash-image",
                        api_keys=["cpa-image-secret"],
                        provider_type="gemini_native",
                    )
                }
            )
            with self.assertRaises(CoverGenerationError):
                generate_cover(
                    title="白雾渡口",
                    pen_name="林栖",
                    output_path=Path(tmp) / "fail_cover.png",
                    base_mode="ai_image",
                    router=router,
                )

    def test_procedural_mode_png_and_jpeg_within_specs_and_title_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            meta = ProjectMeta(
                project_id="demo_book",
                title="让你劈柴十年你劈出了剑意啊",
                genre="玄幻脑洞",
                synopsis="杂役弟子劈柴十年积攒剑意，一朝下山惊动整个修仙界。",
                default_pov_character="zhao",
                pen_name="南山归客",
            )
            png_path = Path(tmp) / "proc.png"
            res_png = generate_cover_for_project(
                meta,
                png_path,
                base_mode="procedural",
                output_format="PNG",
            )
            self.assertEqual((res_png.width, res_png.height), (600, 800))
            self.assertEqual(res_png.mode, "RGB")
            self.assertGreaterEqual(res_png.file_size_bytes, MIN_FILE_SIZE_BYTES)
            self.assertLessEqual(res_png.file_size_bytes, MAX_FILE_SIZE_BYTES)
            self.assertGreaterEqual(res_png.title_bbox[0], SAFE_MARGIN_PX)
            self.assertLessEqual(res_png.title_bbox[2], COVER_WIDTH - SAFE_MARGIN_PX)
            self.assertGreaterEqual(res_png.author_bbox[1], SAFE_MARGIN_PX)
            self.assertLessEqual(res_png.author_bbox[3], COVER_HEIGHT - SAFE_MARGIN_PX)

            jpg_path = Path(tmp) / "proc.jpg"
            res_jpg = generate_cover(
                title="白雾渡口",
                pen_name="江畔旧人",
                genre="文学悬疑",
                output_path=jpg_path,
                base_mode="procedural",
                output_format="JPEG",
            )
            self.assertEqual((res_jpg.width, res_jpg.height), (600, 800))
            self.assertEqual(res_jpg.format, "JPEG")
            self.assertGreaterEqual(res_jpg.file_size_bytes, MIN_FILE_SIZE_BYTES)
            self.assertLessEqual(res_jpg.file_size_bytes, MAX_FILE_SIZE_BYTES)

            with self.assertRaises(CoverGenerationError):
                generate_cover(
                    title="超过十五个字的超长番茄违规书名测试",
                    pen_name="笔名",
                    output_path=Path(tmp) / "too_long.png",
                    base_mode="procedural",
                )


class TestMultiPlatformExporter(unittest.TestCase):
    def _sample_meta(self) -> ProjectMeta:
        return ProjectMeta(
            project_id="urban_hit",
            title="工位刷出百亿",
            genre="都市脑洞",
            genre_id="urban_brainhole",
            synopsis=(
                "实习转正前夜，陈牧野被主管抢走核心方案并被房东限时清空隔断间。"
                "兜里只剩三百块时，他手机弹出每日破局账单，当面拆穿职场黑幕即可十倍返现。"
            ),
            default_pov_character="chen_muye",
            pen_name="青山煮茶",
            primary_category="都市脑洞",
            tags=["系统", "神豪", "职场逆袭"],
            tweet_aliases=["工位破局账单", "百亿工位录", "十倍返现风云"],
        )

    def _sample_characters(self) -> dict[str, CharacterAnchor]:
        return {
            "chen_muye": CharacterAnchor(
                id="chen_muye",
                name="陈牧野",
                role="主角",
                personality="冷静狠辣、算账极准、绝不内耗",
                speech_style="简短带刺，直接甩证据",
            ),
            "lu_zhen": CharacterAnchor(
                id="lu_zhen",
                name="陆振",
                role="反派主管",
                personality="贪婪虚伪、欺软怕硬",
                speech_style="惯用职场画饼与推责话术",
            ),
        }

    def _sample_chapters(self) -> list[ChapterArtifact]:
        return [
            ChapterArtifact(
                chapter=1,
                public_title="第1章 工位移交单",
                body="“下午五点前把工位清干净。”主管把辞退通知推过来，陈牧野低头看着手机里的十倍返现提示，笑了。",
                author_note="新书起航，求加入书架追更！",
            ),
            ChapterArtifact(
                chapter=2,
                public_title="第十二章：全群公开戳穿",
                body="‘你敢群发邮件？’陆振脸色煞白。陈牧野抬手按下发送键，两千人的公司公告群瞬间炸开了锅。",
                author_note="下一章陆振直接社死，别走开！",
            ),
            ChapterArtifact(
                chapter=3,
                public_title="Chapter 3: 拍卖会反杀",
                body="陈牧野走进拍卖厅，前排的陆振还在嘲讽他买不起入场券。下一秒，屏幕显示全场最高举牌人是陈牧野。",
                author_note="",
            ),
            ChapterArtifact(
                chapter=4,
                public_title="04. 神秘资本入场",
                body="散场时，黑衣助理递来一张鎏金名片：'老板在顶楼等您。'而此时监控正拍下陆振偷偷转移账册的背影。",
                author_note="第5章正面清算！",
            ),
        ]

    def test_strip_chapter_prefix_and_tomato_bundle_export(self) -> None:
        self.assertEqual(strip_chapter_title_prefix("第1章 工位移交单"), "工位移交单")
        self.assertEqual(strip_chapter_title_prefix("第十二章：全群公开戳穿"), "全群公开戳穿")
        self.assertEqual(strip_chapter_title_prefix("Chapter 3: 拍卖会反杀"), "拍卖会反杀")
        self.assertEqual(strip_chapter_title_prefix("04. 神秘资本入场"), "神秘资本入场")

        with tempfile.TemporaryDirectory() as tmp:
            res = export_tomato_bundle(
                meta=self._sample_meta(),
                chapters=self._sample_chapters(),
                output_dir=Path(tmp) / "tomato_out",
                characters=self._sample_characters(),
            )
            self.assertEqual(res.chapter_count, 4)
            self.assertEqual(len(res.validated_aliases), 3)
            self.assertTrue(res.zip_path.is_file())

            ch1_json = json.loads((res.chapters_dir / "chapter_0001.json").read_text(encoding="utf-8"))
            ch2_json = json.loads((res.chapters_dir / "chapter_0002.json").read_text(encoding="utf-8"))
            ch3_json = json.loads((res.chapters_dir / "chapter_0003.json").read_text(encoding="utf-8"))
            ch4_json = json.loads((res.chapters_dir / "chapter_0004.json").read_text(encoding="utf-8"))

            self.assertEqual(ch1_json["chapter_no"], 1)
            self.assertEqual(ch1_json["public_title"], "工位移交单")
            self.assertEqual(ch2_json["public_title"], "全群公开戳穿")
            self.assertEqual(ch3_json["public_title"], "拍卖会反杀")
            self.assertEqual(ch4_json["public_title"], "神秘资本入场")
            self.assertNotIn("新书起航", ch1_json["body"])
            self.assertEqual(ch1_json["author_note"], "新书起航，求加入书架追更！")

            ch1_txt = (res.chapters_dir / "chapter_0001.txt").read_text(encoding="utf-8")
            self.assertNotIn("新书起航", ch1_txt)
            self.assertNotIn("工位移交单", ch1_txt)

            card = json.loads(res.project_card_path.read_text(encoding="utf-8"))
            self.assertLessEqual(len(card["title"]), 15)
            self.assertTrue(50 <= len(card["blurb"]) <= 500)
            self.assertEqual(len(card["tags"]), 3)

            tweet = json.loads(res.tweet_kit_path.read_text(encoding="utf-8"))
            self.assertEqual(tweet["stops_before_chapter"], 5)
            self.assertEqual(len(tweet["acts"]), 4)
            self.assertIn("第5章", tweet["script_text"])

            with zipfile.ZipFile(res.zip_path, "r") as zf:
                names = set(zf.namelist())
                self.assertIn("chapters_split/chapter_0001.json", names)
                self.assertIn("chapters_split/chapter_0001.txt", names)
                self.assertIn("project_card.json", names)
                self.assertIn("character_star_cards.json", names)
                self.assertIn("tweet_kit.json", names)

    def test_tomato_rejects_invalid_tweet_alias_and_mixed_author_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(InvalidTweetAliasError):
                export_tomato_bundle(
                    meta=self._sample_meta(),
                    chapters=self._sample_chapters(),
                    output_dir=Path(tmp) / "bad_alias",
                    tweet_aliases=["工位破局1", "百亿工位录", "十倍返现风云"],
                )

            bad_chapters = [
                ChapterArtifact(
                    chapter=1,
                    public_title="第一幕",
                    body="正文内容。\n【作者有话说】加微信号领取番外。",
                    author_note="",
                )
            ]
            with self.assertRaises(ExportValidationError):
                export_tomato_bundle(
                    meta=self._sample_meta(),
                    chapters=bad_chapters,
                    output_dir=Path(tmp) / "bad_note",
                )

    def test_qimao_and_zhihu_yanxuan_exports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            qm = export_qimao_bundle(
                meta=self._sample_meta(),
                chapters=self._sample_chapters(),
                output_dir=Path(tmp) / "qimao",
            )
            qm_text = qm.manuscript_path.read_text(encoding="utf-8")
            self.assertIn("第1章 工位移交单", qm_text)
            self.assertIn("第2章 全群公开戳穿", qm_text)
            self.assertNotIn("第1章 第1章", qm_text)

            zh = export_zhihu_yanxuan_bundle(
                meta=self._sample_meta(),
                chapters=self._sample_chapters(),
                output_dir=Path(tmp) / "zhihu",
            )
            zh_text = zh.manuscript_path.read_text(encoding="utf-8")
            zh_ann = zh.annotated_path.read_text(encoding="utf-8")
            for bad_quote in ("“", "”", "‘", "’", '"'):
                self.assertNotIn(bad_quote, zh_text)
            self.assertIn("「下午五点前把工位清干净。」", zh_text)
            self.assertIn("『你敢群发邮件？』", zh_text)
            lines = zh_text.splitlines()
            self.assertIn("1", lines)
            self.assertIn("2", lines)
            self.assertIn("3", lines)
            self.assertIn("4", lines)
            self.assertIn("PAYWALL_30_PERCENT", zh_ann)
            self.assertIn("30%付费卡点导语", zh_ann)
            self.assertGreater(zh.paywall_char_ratio, 0.0)
            self.assertLess(zh.paywall_char_ratio, 1.0)
            self.assertEqual(convert_to_corner_quotes('他说：“这是‘真品’。”'), "他说：「这是『真品』。」")


class TestEnglishWebnovelTranscreator(unittest.TestCase):
    def test_glossary_rules_unit_elimination_and_anti_chinglish(self) -> None:
        xuanhuan_meta = ProjectMeta(
            project_id="xh_01",
            title="劈柴出剑意",
            genre="玄幻脑洞",
            genre_id="xuanhuan_brainhole",
            synopsis="杂役弟子靠劈柴悟出无敌剑意。",
            default_pov_character="zhao",
        )
        chars = {
            "zhao": CharacterAnchor(
                id="zhao",
                name="赵启明",
                role="主角",
                personality="沉稳内敛",
                speech_style="寡言",
            ),
            "ouyang": CharacterAnchor(
                id="ouyang",
                name="欧阳明",
                role="长老",
                personality="精明",
                speech_style="威严",
            ),
        }
        glossary = build_glossary(xuanhuan_meta, characters=chars)
        self.assertEqual(glossary.track, "eastern_xuanhuan")
        self.assertEqual(glossary.character_names["赵启明"], "Zhao Qiming")
        self.assertEqual(glossary.character_names["欧阳明"], "Ouyang Ming")
        self.assertEqual(glossary.realm_terms["筑基期"], "Foundation Establishment")
        self.assertEqual(glossary.realm_terms["灵石"], "Spirit Stones")

        with tempfile.TemporaryDirectory() as tmp:
            g_path = glossary.save(Path(tmp) / "glossary.json")
            loaded = TranscreationGlossary.load(g_path)
            self.assertEqual(loaded.character_names, glossary.character_names)

        # Urban track: westernized names + USD purchasing power
        urban_meta = ProjectMeta(
            project_id="ub_01",
            title="工位刷出百亿",
            genre="都市脑洞",
            genre_id="urban_brainhole",
            synopsis="职场逆袭神豪。",
            default_pov_character="chen",
        )
        urban_chars = {
            "chen": CharacterAnchor(id="chen", name="陈牧野", role="主角", personality="果断", speech_style="干脆"),
            "lu": CharacterAnchor(id="lu", name="陆振", role="反派", personality="贪婪", speech_style="虚伪"),
        }
        u_glossary = build_glossary(urban_meta, characters=urban_chars)
        self.assertEqual(u_glossary.track, "modern_urban")
        self.assertNotEqual(u_glossary.character_names["陈牧野"], u_glossary.character_names["陆振"])
        self.assertNotIn("陈", u_glossary.character_names["陈牧野"])
        self.assertEqual(convert_cny_to_usd_purchasing_power(1_000_000), "$150,000")
        self.assertEqual(convert_cny_to_usd_purchasing_power(300), "$45")

        # Validate catches bare-pinyin measurements, Chinglish, hyphenated pinyin, and leaked Hanzi
        bad_prose = (
            "Zhao Qi-ming walked ten li and leaped three zhang using 5 cun of sword Qi. "
            "Director Lu shouted at him about 筑基期."
        )
        errs = validate_english_chapter_text(bad_prose, glossary)
        self.assertTrue(any("Bare-pinyin" in e for e in errs))
        self.assertTrue(any("Chinglish" in e for e in errs))
        self.assertTrue(any("Hyphenated" in e for e in errs))
        self.assertTrue(any("Hanzi" in e for e in errs))

        fixed = normalize_localization_text(
            "Zhao Qi-ming walked ten li and leaped three zhang. Director Lu frowned.",
            glossary,
        )
        self.assertNotIn("ten li", fixed.lower())
        self.assertNotIn("three zhang", fixed.lower())
        self.assertNotIn("Director Lu", fixed)
        self.assertIn("Mr. Lu", fixed)
        self.assertIn("Zhao Qiming", fixed)

    def test_transcreator_with_role_model_router_and_bundle_export(self) -> None:
        meta = ProjectMeta(
            project_id="xh_02",
            title="劈柴出剑意",
            genre="玄幻脑洞",
            genre_id="xuanhuan_brainhole",
            synopsis="杂役弟子靠劈柴悟出无敌剑意。",
            default_pov_character="zhao",
        )
        chars = {
            "zhao": CharacterAnchor(id="zhao", name="赵启明", role="主角", personality="沉稳", speech_style="干脆")
        }
        glossary = build_glossary(meta, characters=chars)

        # Missing provider/router raises explicit error
        with self.assertRaises(TranscreationProviderError):
            WebnovelTranscreator(glossary).transcreate_chapter(
                ChapterArtifact(chapter=1, public_title="劈柴", body="赵启明挥动铁斧。")
            )

        clean_en = (
            "Chapter 1: Splitting Ironwood\n\n"
            "Zhao Qiming swung the iron axe down, splitting the stone slab thirty feet wide. "
            "Every strike honed his Foundation Establishment core and saved him Spirit Stones."
        )
        with _FakeCPAServer(sse_content=clean_en) as srv, tempfile.TemporaryDirectory() as tmp:
            router = RoleModelRouter(
                roles={
                    "writer": RoleEndpointConfig(
                        role="writer",
                        base_url=f"{srv.base_url}/v1",
                        model="gpt-test",
                        api_keys=["sk-transcreate-1234"],
                    )
                }
            )
            tc = WebnovelTranscreator(glossary, router=router)
            art = ChapterArtifact(
                chapter=1,
                public_title="铁斧劈石",
                body="赵启明挥斧劈开三丈青石，筑基期灵力暴涨，省下十块灵石。",
            )
            ch_en = tc.transcreate_chapter(art)
            self.assertEqual(ch_en.chapter, 1)
            self.assertEqual(ch_en.title_en, "Splitting Ironwood")

            bundle = export_english_serialization_bundle([ch_en], glossary, Path(tmp) / "en_bundle")
            ch_file_text = (bundle.chapters_dir / "chapter_0001.txt").read_text(encoding="utf-8")
            self.assertTrue(ch_file_text.startswith("Chapter 1: Splitting Ironwood\n\n"))
            self.assertTrue(bundle.glossary_path.is_file())
            self.assertTrue(bundle.manuscript_path.is_file())

        # Missing required glossary character in English output triggers GlossaryValidationError
        bad_en = (
            "Chapter 1: Missing Name\n\n"
            "An unnamed youth swung the iron axe at the Foundation Establishment stage."
        )
        with _FakeCPAServer(sse_content=bad_en) as bad_srv:
            router = RoleModelRouter(
                roles={
                    "writer": RoleEndpointConfig(
                        role="writer",
                        base_url=f"{bad_srv.base_url}/v1",
                        model="gpt-test",
                        api_keys=["sk-transcreate-1234"],
                    )
                }
            )
            tc = WebnovelTranscreator(glossary, router=router)
            with self.assertRaises(GlossaryValidationError):
                tc.transcreate_chapter(
                    ChapterArtifact(chapter=1, public_title="劈柴", body="赵启明劈开青石。")
                )


if __name__ == "__main__":
    unittest.main()
