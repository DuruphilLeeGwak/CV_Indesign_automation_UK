# CV Automation System

A data-driven pipeline that generates tailored CV/Cover Letter `.idml` files for each job application — no manual InDesign edits required.

## Purpose

Keeping a single InDesign template while applying for multiple roles means copy-pasting and reformatting the same document repeatedly. This tool separates **content from layout**: personal info and job-specific copy live in plain TOML files, and the script injects them into the template to produce a ready-to-open IDML file.

## How it works

1. `me.toml` — fixed personal information (name, contact, skills, work history)
2. `<Company>_<Position>.toml` — one file per application (job details, tailored cover letter body)
3. Run `python inject_idml.py` → outputs `output/<Name>_<Company>_<Position>.idml`
4. Open in InDesign → export to PDF

## Key features

- **One command, one file** — drop a single job TOML in the root and run the script; the output filename is derived automatically
- **Rich text via XML** — hyperlinks, bullet lists with hanging indents, and named paragraph/character styles are all handled programmatically
- **Clean separation** — personal data (`me.toml`) and job data are never mixed into the template itself, making the template reusable across applications

## Requirements

```bash
pip install lxml
```

An InDesign IDML template (`template/WS_Template.idml`) is required locally and is not included in this repository.
