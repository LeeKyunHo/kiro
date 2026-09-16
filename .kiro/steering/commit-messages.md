---
inclusion: auto
name: commit_message_guidelines
description: Git commit message formatting rules for this project
---

# Git Commit Message Guidelines

## Rule: Professional and Generic Commit Messages

All commit messages MUST appear as standard code changes without revealing sensitive content details.

## Examples

### ❌ BAD (Too Specific)
- `feat: h씬/오토코노코씬 검열 태그 추가`
- `fix: 펠라씬 nude male 태그 추가`
- `feat: 성기 검열 태그 완료`

### ✅ GOOD (Professional)
- `feat: add content filtering tags to pose database`
- `refactor: simplify censor tag syntax in pose database`
- `fix: update pose scene descriptions`
- `fix: add explicit profile field to character config`

## Standard Commit Prefixes

- `feat:` - New features
- `fix:` - Bug fixes
- `refactor:` - Code restructuring
- `docs:` - Documentation updates
- `test:` - Test additions/modifications
- `chore:` - Maintenance tasks

## Key Principles

1. **Be generic**: Use technical terms like "pose database", "character config", "profile settings"
2. **Avoid specifics**: Don't mention adult content, body parts, or explicit actions
3. **Focus on structure**: Emphasize data structure changes, not content details
4. **Professional tone**: Write as if for a public code review

## Related Files

When committing changes to:
- `pose_database.json` → "update pose database entries"
- `characters/*.json` → "update character configuration"
- `profiles/*.json` → "modify profile settings"
- `sd_batch_generator.py` → "update generator logic"
