"""MCP (Model Context Protocol) Server for METU Student Portal Course Details."""

from typing import List, Optional, Dict, Any
from mcp.server.mcpserver import MCPServer

from .sais_client import SAISClient, get_cached_client
from .models import (
    Department,
    Semester,
    DepartmentAndSemesterList,
    CourseSummary,
    CourseDetails,
    SectionConstraints,
    CoursePrerequisite,
    CourseReplacement,
    ThesisCourse,
    StudentCategoryOverview,
    StudentCategoryResult,
)


def create_mcp_server() -> MCPServer:
    """Create and configure the METU Course Info MCP server."""
    server = MCPServer("metu-course-info")

    @server.tool(
        name="get_departments_and_semesters",
        description=(
            "Retrieve all available METU academic departments (code and name) and semester terms "
            "(e.g., '20251' for 2025-2026 Fall) from Program Course Details (64)."
        ),
    )
    async def get_departments_and_semesters() -> DepartmentAndSemesterList:
        client = get_cached_client()
        return await client.get_departments_and_semesters()

    @server.tool(
        name="search_departments",
        description=(
            "Search for METU department codes by keyword (e.g. 'Computer', 'Economics', 'Aerospace', '571')."
        ),
    )
    async def search_departments(query: str) -> List[Department]:
        client = get_cached_client()
        data = await client.get_departments_and_semesters()
        q = query.lower().strip()
        results = [
            d for d in data.departments
            if q in d.code.lower() or q in d.name.lower()
        ]
        return results

    @server.tool(
        name="list_program_courses",
        description=(
            "List all offered courses for a given METU department code and semester code. "
            "Returns Course Code, Course Name, ECTS Credits, Credits, Level, and Type."
        ),
    )
    async def list_program_courses(
        department_code: str,
        semester_code: str,
    ) -> List[CourseSummary]:
        client = get_cached_client()
        return await client.list_program_courses(
            department_code=department_code,
            semester_code=semester_code,
        )

    @server.tool(
        name="get_course_info",
        description=(
            "Retrieve detailed course information for a specific METU course, including sections, "
            "instructors, syllabus status, announcements, and lecture schedules."
        ),
    )
    async def get_course_info(
        department_code: str,
        semester_code: str,
        course_code: str,
    ) -> CourseDetails:
        client = get_cached_client()
        return await client.get_course_info(
            department_code=department_code,
            semester_code=semester_code,
            course_code=course_code,
        )

    @server.tool(
        name="get_section_constraints",
        description=(
            "Retrieve the eligibility table for one section of a METU course: which departments "
            "may register, and the surname range, cumulative GPA range and year-of-study range "
            "each of them is limited to. A department absent from the table cannot take the section."
        ),
    )
    async def get_section_constraints(
        department_code: str,
        semester_code: str,
        course_code: str,
        section: str,
    ) -> SectionConstraints:
        client = get_cached_client()
        return await client.get_section_constraints(
            department_code=department_code,
            semester_code=semester_code,
            course_code=course_code,
            section=section,
        )

    @server.tool(
        name="get_course_prerequisites",
        description=(
            "Retrieve all prerequisite course requirements, prerequisite set groups, and minimum "
            "required letter grades (e.g., DD) for a specific METU course."
        ),
    )
    async def get_course_prerequisites(
        department_code: str,
        semester_code: str,
        course_code: str,
    ) -> List[CoursePrerequisite]:
        client = get_cached_client()
        return await client.get_course_prerequisites(
            department_code=department_code,
            semester_code=semester_code,
            course_code=course_code,
        )

    @server.tool(
        name="get_course_replacements",
        description=(
            "Retrieve equivalent and auto-replacement courses (Denk Dersler) for a specific METU course."
        ),
    )
    async def get_course_replacements(
        department_code: str,
        semester_code: str,
        course_code: str,
    ) -> List[CourseReplacement]:
        client = get_cached_client()
        return await client.get_course_replacements(
            department_code=department_code,
            semester_code=semester_code,
            course_code=course_code,
        )

    @server.tool(
        name="get_thesis_courses",
        description=(
            "Retrieve all thesis work courses for a department and semester term."
        ),
    )
    async def get_thesis_courses(
        department_code: str,
        semester_code: str,
    ) -> List[ThesisCourse]:
        client = get_cached_client()
        return await client.get_thesis_courses(
            department_code=department_code,
            semester_code=semester_code,
        )

    @server.tool(
        name="get_student_course_categories",
        description=(
            "Retrieve the logged-in student's program types (e.g. MAJOR) and curriculum course categories "
            "(MUST COURSE, DEPARTMENTAL ELECTIVE, NONDEPARTMENTAL ELECTIVE, FREE ELECTIVE) from Service 178."
        ),
    )
    async def get_student_course_categories() -> StudentCategoryOverview:
        client = get_cached_client()
        return await client.get_student_categories_overview()

    @server.tool(
        name="get_student_courses_by_category",
        description=(
            "Retrieve the list of courses belonging to a student's category (e.g. MUST COURSES or "
            "DEPARTMENTAL ELECTIVES with category_id like '1-236' or '2-236')."
        ),
    )
    async def get_student_courses_by_category(
        program_type: str = "1",
        category_id: str = "1-236",
    ) -> StudentCategoryResult:
        client = get_cached_client()
        return await client.get_student_category_courses(
            program_type=program_type,
            category_id=category_id,
        )

    return server
