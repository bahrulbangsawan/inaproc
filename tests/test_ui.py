import io
import os
import unittest
from unittest import mock

import tests._util  # noqa: F401
import inaproc_ui as ui


class Tty(io.StringIO):
    def isatty(self):
        return True


class StyleTest(unittest.TestCase):
    def test_no_color_when_not_a_tty_or_no_color_set(self):
        self.assertEqual(ui.Style(io.StringIO()).bold("x"), "x")
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertEqual(ui.Style(Tty()).bold("x"), "x")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NO_COLOR", None)
            self.assertIn("\x1b[1m", ui.Style(Tty()).bold("x"))

    def test_visible_width_ignores_ansi(self):
        self.assertEqual(ui.vlen("\x1b[1mabc\x1b[0m"), 3)


class LogoTest(unittest.TestCase):
    def test_logo_spells_inaproc_and_fits_80_columns(self):
        out = io.StringIO()
        ui.banner("1.4.0", stream=out)
        text = out.getvalue()
        self.assertIn("1.4.0", text)
        self.assertTrue(all(ui.vlen(l) <= 80 for l in text.splitlines()))
        self.assertGreaterEqual(len(ui.LOGO), 5)


class TableTest(unittest.TestCase):
    def test_box_table_aligns_columns(self):
        out = ui.table(["No", "Produk"], [[1, "Kertas"], [10, "Laptop Core i5"]], color=False)
        lines = out.splitlines()
        self.assertEqual(len({ui.vlen(l) for l in lines}), 1)  # every line the same width
        self.assertIn("Laptop Core i5", out)
        self.assertTrue(lines[0].startswith("╭"))

    def test_long_cells_are_cut_to_fit(self):
        out = ui.table(["A"], [["x" * 500]], color=False, max_width=60)
        self.assertTrue(all(ui.vlen(l) <= 60 for l in out.splitlines()))


class CardsTest(unittest.TestCase):
    URL = "https://katalog.inaproc.id/raka-kencana/kertas-hvs-a4-80-gram-panjang-sekali"
    HEAD = ["#", "Product", "Price", "Seller", "Location", "TKDN+BMP", "Kategori L1", "Kategori L2", "Kategori L3",
            "URL"]

    def _row(self, n):
        return [str(n), "Kertas HVS A4 80 gram", "Rp48.000", "RAKA KENCANA", "Margahayu", "84.83%",
                "Perlengkapan Alat Tulis", "Buku Tulis", "Buku Tulis Lainnya", self.URL]

    def test_few_wide_rows_become_a_sideways_table(self):
        out = ui.auto_table(self.HEAD, [self._row(1), self._row(2)], color=False, max_width=100)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("╭"))
        self.assertTrue(any(l.startswith("│ Seller") for l in lines))   # fields become rows
        self.assertIn(self.URL, out)                                    # never cut
        self.assertIn("Buku Tulis Lainnya", out)

    def test_many_wide_rows_become_one_detail_table_each(self):
        out = ui.auto_table(self.HEAD, [self._row(i) for i in range(1, 8)], color=False, max_width=80)
        self.assertEqual(out.count("╭"), 7)
        self.assertIn("│ Seller", out)
        self.assertIn(self.URL, out)

    def test_detail_tables_share_one_width(self):
        rows = [self._row(i)[:-1] for i in range(1, 8)]              # no link column
        rows[3] = ["4", "Map", "Rp1", "A", "B", "-", "C", "D", "E"]  # one row with only short values
        out = ui.auto_table(self.HEAD[:-1], rows, color=False, max_width=100)
        tops = [l for l in out.splitlines() if l.startswith("╭")]
        self.assertEqual(len(tops), 7)
        self.assertEqual(len({ui.vlen(t) for t in tops}), 1)

    def test_narrow_tables_stay_tables(self):
        out = ui.auto_table(["No", "Produk"], [["1", "Kertas"]], color=False, max_width=80)
        self.assertTrue(out.startswith("╭"))

    def test_key_value_table_has_no_empty_header(self):
        out = ui.kv_table([["Penyedia", "BAHRUL"], ["AI", "belum diatur"]], color=False)
        self.assertEqual(len(out.splitlines()), 4)  # top, 2 rows, bottom
        self.assertIn("BAHRUL", out)

    def test_headerless_markdown_table_renders_as_key_value(self):
        out = ui.auto_table(["", ""], [["Status", "Selesai"], ["Total", "Rp1"]], color=False, max_width=80)
        self.assertEqual(len(out.splitlines()), 4)


class StepsTest(unittest.TestCase):
    def test_progress_detail_does_not_replace_the_step_name(self):
        err = io.StringIO()
        with ui.Steps(stream=err) as s:
            s.start("Mencari produk termurah")
            ui.progress("Katalog · searchProducts")
            s.done("3 hasil")
        text = err.getvalue()
        self.assertIn("Mencari produk termurah", text)
        self.assertNotIn("searchProducts", text.splitlines()[-1].split("—")[0])

    def test_plain_steps_print_start_and_done(self):
        err = io.StringIO()
        with ui.Steps(stream=err) as s:
            s.start("Mengambil produk")
            s.done("30 produk")
            s.start("Membaca kategori")
            s.fail("koneksi putus")
        text = err.getvalue()
        self.assertIn("Mengambil produk", text)
        self.assertIn("30 produk", text)
        self.assertIn("koneksi putus", text)
        self.assertIn("✓", text)
        self.assertIn("✗", text)

    def test_update_changes_the_running_label(self):
        err = io.StringIO()
        with ui.Steps(stream=err) as s:
            s.start("Kategori 0/100")
            s.update("Kategori 50/100")
            s.done()
        self.assertIn("Kategori 50/100", err.getvalue())


class PromptTest(unittest.TestCase):
    def test_choose_by_number_and_default(self):
        with mock.patch("builtins.input", side_effect=["2"]):
            self.assertEqual(ui.choose("Pilih", ["a", "b", "c"], stream=io.StringIO()), 1)
        with mock.patch("builtins.input", side_effect=[""]):
            self.assertEqual(ui.choose("Pilih", ["a", "b"], default=1, stream=io.StringIO()), 1)
        with mock.patch("builtins.input", side_effect=["9", "x", "1"]):
            self.assertEqual(ui.choose("Pilih", ["a", "b"], stream=io.StringIO()), 0)

    def test_confirm_defaults_to_no(self):
        with mock.patch("builtins.input", side_effect=[""]):
            self.assertFalse(ui.confirm("Kirim?", stream=io.StringIO()))
        with mock.patch("builtins.input", side_effect=["ya"]):
            self.assertTrue(ui.confirm("Kirim?", stream=io.StringIO()))

    def test_mask_secret(self):
        self.assertEqual(ui.mask("sk-ant-api03-abcdefgh1234"), "sk-ant-…1234")
        self.assertEqual(ui.mask(""), "")


class PanelTest(unittest.TestCase):
    def test_panel_borders_line_up(self):
        out = io.StringIO()
        ui.panel("Safari belum siap", ["1. Buka Safari", "2. Centang opsi yang panjang sekali"], kind="warn", stream=out)
        widths = {ui.vlen(l) for l in out.getvalue().splitlines()}
        self.assertEqual(len(widths), 1, out.getvalue())


class ColorOffTest(unittest.TestCase):
    def test_color_false_means_no_escape_codes_even_on_a_terminal(self):
        with mock.patch("sys.stdout", Tty()), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NO_COLOR", None)
            os.environ.pop("INAPROC_PLAIN", None)
            self.assertNotIn("\x1b", ui.kv_table([["A", "b"]], color=False))
            self.assertNotIn("\x1b", ui.auto_table(["A", "B"], [["1", "2"]], color=False))
            self.assertNotIn("\x1b", ui.render_markdown("**x** `y`", color=False))


class WrapTest(unittest.TestCase):
    def test_wrap_keeps_leading_indent(self):
        self.assertEqual(ui._wrap("       inaproc ai", 40), ["       inaproc ai"])


class MarkdownTest(unittest.TestCase):
    def test_render_bold_and_headings_plain(self):
        out = ui.render_markdown("## Hasil\n**Termurah**: Rp48.000", color=False)
        self.assertIn("Hasil", out)
        self.assertIn("Termurah: Rp48.000", out)
        self.assertNotIn("**", out)


if __name__ == "__main__":
    unittest.main()
