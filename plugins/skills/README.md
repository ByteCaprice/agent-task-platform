# Example Skills

Skills are deployed artifacts, not Python plugins. Each Skill directory contains `SKILL.md`, optional `references/` or `assets/`, and optional governed scripts declared in `agents/platform.yaml`.

The platform loads artifacts from this trusted root, stores immutable versions in the database, and exposes them through the shared Skill runtime. Do not import a Skill module directly or add organization-specific policy, credentials, or integration logic here. Keep example content synthetic.
