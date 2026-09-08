# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Devrimo is for METU students who want to complete routine academic tasks with less effort. Its primary audience is ordinary students managing courses, schedules, deadlines, campus information, and related communication rather than specialist or administrative users.

## Product Purpose

Devrimo brings student-specific academic planning and campus assistance into one conversational product. It helps a student understand and organize their semester, build a workable course schedule, follow relevant campus developments, and use connected METU services without moving between several systems.

Success means giving the student a correct, useful next step quickly while preserving their control over which institutional data the product may access.

## Positioning

Devrimo combines a conversational assistant with verified, student-specific METU context. It can use SAIS, the METU course catalog, ODTUClass, and optional METU email access, while deterministic planning rules handle eligibility, prerequisites, conflicts, and credit limits. Academic claims must come from current institutional data rather than model inference.

## Operating Context

Students use Devrimo on desktop and mobile web, primarily in Turkish or English. Core workflows include:

- asking questions and planning work through chat;
- loading the student's SAIS curriculum, transcript, academic identity, and schedule;
- selecting required courses and comparing eligible sections in the schedule planner;
- reviewing campus announcements and updates;
- optionally reading or searching ODTUClass and METU email information;
- reviewing and controlling connections, stored academic data, preferences, and memories in Settings.

## Capabilities and Constraints

- SAIS is the authority for the student's academic identity, curriculum, transcript, and registered schedule.
- The course planner must use verified institutional data and deterministic rules. The model cannot supply grades, invent prerequisites, or mark a prerequisite as satisfied.
- Required-course suggestions come from the student's curriculum. Electives are added manually by the student.
- ODTUClass and METU email access are optional and require explicit permission.
- Sending or replying to email requires confirmation of the exact action before it is performed.
- METU credentials are verified before storage, encrypted at rest, excluded from response schemas, and removable from Settings.
- Turkish and English are supported product languages.
- The interface must remain usable on mobile web as well as desktop.
- The planner provides decision support and does not enroll the student in courses.

## Brand Commitments

The product name is **Devrimo**. Its voice is direct, practical, student-friendly, and clear about data access. It should reduce the work of navigating university systems without overstating certainty or hiding the source of academic information.

## Evidence on Hand

- The production interface and product copy live under `frontend/app` and `frontend/components`.
- The authenticated student workflows are implemented in chat, schedule, updates, onboarding, and settings surfaces.
- Backend architecture and verified constraints are documented in `backend/README.md`.
- Campus-data ingestion and academic-planning behavior are documented in `docs/campus-intelligence.md`.
- Email authority and confirmation boundaries are documented in `docs/decisions/0001-webmail-write-authority.md`.
- Automated backend tests include curriculum parsing, eligibility, prerequisites, transcript handling, privacy, retention, and student isolation.
- No testimonials, customer claims, institutional endorsement, pricing claims, or public performance benchmarks are established in the repository and they must not be fabricated.

## Product Principles

1. Use verified student context before giving academic guidance.
2. Make the routine path fast for an ordinary student and leave exceptional choices under their control.
3. Explain exclusions, prerequisites, permissions, and consequential actions in plain language.
4. Ask only for the access needed for the requested task and keep optional sources optional.
5. Preserve the student's work and choices across sessions whenever doing so remains accurate and safe.

## Accessibility & Inclusion

Devrimo must provide a usable responsive web experience in Turkish and English. Controls require clear accessible names, keyboard-operable interactions, readable contrast in light and dark themes, and layouts that remain functional on mobile screens.
