import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bundle
from bundle import BundleError, create_bundle


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        self.destination = self.base / "bundle"

    def write(self, name, contents=""):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return path

    def bundle(self, entry="main.tex", assets=None):
        return create_bundle(self.root / entry, self.root, self.destination, assets)

    def output_files(self):
        return {path.relative_to(self.destination).as_posix() for path in self.destination.rglob("*") if path.is_file()}

    def test_recursive_shared_inputs_do_not_copy_unrelated_files(self):
        self.write("main.tex", r"\input{parts/one}\include{parts/two}")
        self.write("parts/one.tex", r"\input{shared}")
        self.write("parts/two.tex", r"\input{shared}")
        self.write("shared.tex", "A shared lemma.")
        self.write("old-draft.tex", "Private old draft")
        self.write(".env", "PRIVATE_TOKEN=secret")
        self.write(".codex/config.toml", "sensitive")
        self.write(".audit-work/old/main.tex", "history")
        self.assertEqual(self.bundle(), "main.tex")
        self.assertEqual(self.output_files(), {"main.tex", "parts/one.tex", "parts/two.tex", "shared.tex"})

    def test_multiline_comments_and_literal_examples(self):
        self.write("main.tex", "\\input % ignore \\input{missing}\n {parts/one}\n"
                   "% \\input{missing}\n"
                   "\\verb|\\input{missing}|\n"
                   "\\begin{verbatim}\n\\input{missing}\n\\end{verbatim}\n"
                   "Escaped percent \\% \\input{two}\n")
        self.write("parts/one.tex")
        self.write("two.tex")
        self.bundle()
        self.assertEqual(self.output_files(), {"main.tex", "parts/one.tex", "two.tex"})

    def test_comment_within_filename_and_bare_input(self):
        self.write("main.tex", "\\input{chap% comment removes the newline\nter}\n\\input other.tex\n")
        self.write("chapter.tex")
        self.write("other.tex")
        self.bundle()
        self.assertEqual(self.output_files(), {"main.tex", "chapter.tex", "other.tex"})

    def test_literal_percent_does_not_hide_bibliography(self):
        self.write("refs.bib", "@book{example, title={Example}}")
        for index, literal in enumerate((r"\verb|%|", r"\verb*+%\input{missing}+",
                                         r"\Verb!%\input{missing}!", r"\verb%text%")):
            with self.subTest(literal=literal):
                self.destination = self.base / f"bundle-{index}"
                source = literal + " % ignore \\input{missing}\n\\bibliography{refs}\n"
                self.write("main.tex", source)
                self.bundle()
                self.assertEqual(self.output_files(), {"main.tex", "refs.bib"})
                self.assertEqual((self.destination / "main.tex").read_text(), source)

    def test_literal_environments_preserve_percent_and_ignore_fake_inputs(self):
        self.write("refs.bib")
        for index, environment in enumerate(sorted(bundle._LITERAL_ENVIRONMENTS)):
            with self.subTest(environment=environment):
                self.destination = self.base / f"bundle-{index}"
                arguments = {"minted": "{text}", "filecontents": "{sample.txt}",
                             "filecontents*": "{sample.txt}"}.get(environment, "")
                source = ("\\begin{" + environment + "}" + arguments + "\n% literal percent\n"
                          "\\verb|%|\n\\input{missing}\n\\end{" + environment + "}\n"
                          "\\bibliography{refs}\n")
                self.assertEqual(bundle._without_comments(source), source)
                self.write("main.tex", source)
                self.bundle()
                self.assertEqual(self.output_files(), {"main.tex", "refs.bib"})

    def test_comments_around_literal_environment_header(self):
        source = ("% ignore \\verb| and \\begin{verbatim}\n"
                  "\\begin% header comment\r\n{verba% } ignored brace\rtim}\n"
                  "\\verb|%|\n\\input{missing}\n\\end{verbatim}\n\\bibliography{refs}\n")
        self.write("main.tex", source)
        self.write("refs.bib")
        self.bundle()
        self.assertEqual(self.output_files(), {"main.tex", "refs.bib"})

    def test_comment_escaping_and_line_endings(self):
        for newline in ("\n", "\r\n", "\r"):
            with self.subTest(newline=newline):
                source = (r"\% keep" + newline + r"\\% drop \verb|" + newline
                          + r"\\\% keep" + newline + r"\\verb|% drop" + newline
                          + "tail% drop at end")
                expected = r"\% keep" + newline + r"\\" + r"\\\% keep" + newline + r"\\verb|tail"
                self.assertEqual(bundle._without_comments(source), expected)

    def test_bibliography_graphics_class_and_package_dependencies(self):
        self.write("main.tex", r"\documentclass{local}\usepackage[option]{localstyle,amsmath}"
                   r"\bibliography{refs,more}\addbibresource[location=local]{third.bib}"
                   r"\bibliographystyle{custom}\graphicspath{{figures/}}"
                   r"\includegraphics[width=2cm]{plot}\lstinputlisting{code/example.py}")
        self.write("local.cls", r"\LoadClass{article}\RequirePackage{other}")
        self.write("localstyle.sty", r"\input{config.def}")
        self.write("other.sty")
        self.write("config.def")
        self.write("refs.bib")
        self.write("more.bib")
        self.write("third.bib")
        self.write("custom.bst")
        self.write("figures/plot.pdf", "image")
        self.write("figures/unrelated.pdf", "unrelated")
        self.write("code/example.py", "print(1)")
        self.bundle()
        self.assertEqual(self.output_files(), {"main.tex", "local.cls", "localstyle.sty", "other.sty",
                         "config.def", "refs.bib", "more.bib", "third.bib", "custom.bst",
                         "figures/plot.pdf", "code/example.py"})

    def test_import_and_subimport(self):
        self.write("main.tex", r"\import{chapters/}{one.tex}")
        self.write("chapters/one.tex", r"\input{lemma}\subimport{nested/}{two}")
        self.write("chapters/lemma.tex")
        self.write("chapters/nested/two.tex", r"\input{proof}")
        self.write("chapters/nested/proof.tex")
        self.bundle()
        self.assertEqual(self.output_files(), {"main.tex", "chapters/one.tex", "chapters/lemma.tex",
                         "chapters/nested/two.tex", "chapters/nested/proof.tex"})

    def test_nested_entry_uses_project_root_as_compiler_directory(self):
        self.write("paper/main.tex", r"\input{shared}")
        self.write("shared.tex")
        self.assertEqual(self.bundle("paper/main.tex"), "paper/main.tex")
        self.assertEqual(self.output_files(), {"paper/main.tex", "shared.tex"})

    def test_missing_required_dependency_fails_before_copying(self):
        for command in (r"\input{missing}", r"\bibliography{missing}", r"\includegraphics{missing}"):
            with self.subTest(command=command):
                self.write("main.tex", command)
                with self.assertRaisesRegex(BundleError, "Missing"):
                    self.bundle()
                self.assertFalse(self.destination.exists())

    def test_traversal_is_rejected_even_if_target_is_inside_root(self):
        self.write("main.tex", r"\input{sub/../private}")
        self.write("private.tex")
        with self.assertRaisesRegex(BundleError, "traversal"):
            self.bundle()

    def test_external_entry_and_assets_are_rejected(self):
        self.write("main.tex")
        external = self.base / "secret.tex"
        external.write_text("secret")
        with self.assertRaisesRegex(BundleError, "outside"):
            create_bundle(external, self.root, self.destination)
        with self.assertRaisesRegex(BundleError, "outside"):
            self.bundle(assets=[external])

    def test_project_root_symlinks_are_rejected_including_ancestors(self):
        self.write("main.tex")
        linked_root = self.base / "linked-project"
        linked_root.symlink_to(self.root, target_is_directory=True)
        linked_parent = self.base / "linked-parent"
        linked_parent.symlink_to(self.base, target_is_directory=True)
        for root in (linked_root, linked_parent / "project"):
            with self.subTest(root=root), self.assertRaises(BundleError):
                create_bundle(root / "main.tex", root, self.destination)
        self.assertFalse(self.destination.exists())

    def test_project_root_swapped_after_validation_is_never_read(self):
        self.write("main.tex", "A valid manuscript")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "main.tex").write_text("DO_NOT_COPY_SECRET")
        actual_safe_read = bundle.safe_read

        def replace_before_read(root, relative):
            self.root.rename(self.base / "original-project")
            self.root.symlink_to(outside, target_is_directory=True)
            return actual_safe_read(root, relative)

        with patch.object(bundle, "safe_read", side_effect=replace_before_read):
            with self.assertRaises(BundleError):
                self.bundle()
        self.assertFalse(self.destination.exists())

    def test_source_file_and_directory_symlinks_are_rejected(self):
        self.write("main.tex", r"\input{linked}")
        real = self.write("real.tex")
        (self.root / "linked.tex").symlink_to(real)
        with self.assertRaisesRegex(BundleError, "symlink"):
            self.bundle()
        self.write("main.tex", r"\input{linked_dir/chapter}")
        self.write("real_dir/chapter.tex")
        (self.root / "linked_dir").symlink_to(self.root / "real_dir", target_is_directory=True)
        with self.assertRaisesRegex(BundleError, "symlink"):
            self.bundle()

    def test_hardlinked_source_is_rejected(self):
        self.write("main.tex", r"\input{linked}")
        external = self.base / "secret.tex"
        external.write_text("Sensitive data outside project")
        os.link(external, self.root / "linked.tex")
        with self.assertRaisesRegex(BundleError, "hard links"):
            self.bundle()

    def test_symlink_swapped_after_path_validation_is_never_read(self):
        main = self.write("main.tex", "A valid manuscript")
        outside = self.base / "secret.tex"
        outside.write_text("DO_NOT_COPY_SECRET")
        actual_safe_read = bundle.safe_read

        def replace_before_read(root, relative):
            main.unlink()
            main.symlink_to(outside)
            return actual_safe_read(root, relative)

        with patch.object(bundle, "safe_read", side_effect=replace_before_read):
            with self.assertRaises(BundleError):
                self.bundle()
        self.assertFalse(self.destination.exists())

    def test_binary_dependency_swapped_after_discovery_is_never_copied(self):
        self.write("main.tex", r"\includegraphics{plot}")
        graphic = self.write("plot.pdf", "Original graphic")
        outside = self.base / "secret.pdf"
        outside.write_text("DO_NOT_COPY_SECRET")
        original_scan = bundle._Bundle.scan

        def scan_then_replace(scanner, relative, base=Path(".")):
            original_scan(scanner, relative, base)
            graphic.unlink()
            graphic.symlink_to(outside)

        with patch.object(bundle._Bundle, "scan", scan_then_replace):
            with self.assertRaisesRegex(BundleError, "symlink"):
                self.bundle()
        self.assertFalse((self.destination / "plot.pdf").exists())

    def test_reserved_control_and_history_paths_are_rejected(self):
        for directory in (".codex", ".agents", ".git", ".audit-work"):
            with self.subTest(directory=directory):
                self.write("main.tex", "\\input{" + directory + "/private}")
                self.write(directory + "/private.tex", "private")
                with self.assertRaisesRegex(BundleError, "Reserved"):
                    self.bundle()

    def test_dynamic_input_requires_explicit_individual_assets(self):
        self.write("main.tex", r"\def\chapter{parts/one}\input{\chapter}")
        asset = self.write("parts/one.tex", r"\input{shared}")
        self.write("shared.tex")
        self.write("parts/old.tex")
        with self.assertRaisesRegex(BundleError, "--asset"):
            self.bundle()
        self.bundle(assets=[asset])
        self.assertEqual(self.output_files(), {"main.tex", "parts/one.tex", "shared.tex"})

    def test_dynamic_graphics_path_accepts_explicit_asset(self):
        self.write("main.tex", r"\def\figdir{figures}\graphicspath{{\figdir/}}\includegraphics{plot}")
        asset = self.write("figures/plot.pdf")
        self.bundle(assets=[asset])
        self.assertEqual(self.output_files(), {"main.tex", "figures/plot.pdf"})

    def test_entire_graphics_path_list_can_be_a_macro_with_assets(self):
        self.write("main.tex", r"\graphicspath{\mydirs}\includegraphics{plot}")
        asset = self.write("figures/plot.pdf")
        self.bundle(assets=[asset])
        self.assertEqual(self.output_files(), {"main.tex", "figures/plot.pdf"})

    def test_explicit_assets_do_not_allow_dynamic_parent_traversal(self):
        self.write("main.tex", r"\input{../\name}")
        asset = self.write("one.tex")
        with self.assertRaisesRegex(BundleError, "traversal"):
            self.bundle(assets=[asset])

    def test_explicit_symlink_and_reserved_assets_are_rejected(self):
        self.write("main.tex")
        real = self.write("one.tex")
        linked = self.root / "linked.tex"
        linked.symlink_to(real)
        with self.assertRaisesRegex(BundleError, "symlink"):
            self.bundle(assets=[linked])
        control = self.write(".agents/private.md")
        with self.assertRaisesRegex(BundleError, "Reserved"):
            self.bundle(assets=[control])

    def test_custom_graphics_extension_order(self):
        self.write("main.tex", r"\DeclareGraphicsExtensions{.png,.pdf}\includegraphics{plot}")
        self.write("plot.pdf")
        self.write("plot.png")
        self.bundle()
        self.assertEqual(self.output_files(), {"main.tex", "plot.png"})

    def test_graphics_extension_changes_follow_source_and_recursive_input_order(self):
        self.write("main.tex", r"\includegraphics{before}\input{settings}"
                   r"\includegraphics{after}\DeclareGraphicsExtensions{.pdf,.png}"
                   r"\includegraphics{reset}")
        self.write("settings.tex", r"\DeclareGraphicsExtensions{.png,.pdf}")
        for name in ("before", "after", "reset"):
            self.write(name + ".pdf")
            self.write(name + ".png")
        self.bundle()
        self.assertEqual(self.output_files(),
                         {"main.tex", "settings.tex", "before.pdf", "after.png", "reset.pdf"})

    def test_optional_system_files_are_not_required(self):
        self.write("main.tex", r"\documentclass{article}\usepackage{amsmath}"
                   r"\bibliographystyle{plain}\InputIfFileExists{optional.cfg}{}{}")
        self.bundle()
        self.assertEqual(self.output_files(), {"main.tex"})

    def test_explicit_directory_is_not_recursively_copied(self):
        self.write("main.tex")
        self.write("assets/a.tex")
        with self.assertRaisesRegex(BundleError, "regular file"):
            self.bundle(assets=[self.root / "assets"])

    def test_nonempty_destination_is_rejected(self):
        self.write("main.tex")
        self.destination.mkdir()
        (self.destination / "stale.tex").write_text("old")
        with self.assertRaisesRegex(BundleError, "empty"):
            self.bundle()


if __name__ == "__main__":
    unittest.main()
