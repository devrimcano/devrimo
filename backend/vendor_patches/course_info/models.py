"""Pydantic data models for METU Course Info MCP Server."""

from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class Department(BaseModel):
    code: str = Field(description="Department code (e.g. '571')")
    name: str = Field(description="Department name (e.g. 'Computer Engineering/Bilgisayar Mühendisliği')")


class Semester(BaseModel):
    code: str = Field(description="Semester code (e.g. '20251')")
    name: str = Field(description="Semester label (e.g. '2025-2026 Fall')")


class DepartmentAndSemesterList(BaseModel):
    departments: List[Department]
    semesters: List[Semester]


class CourseSummary(BaseModel):
    course_code: str = Field(description="METU Course Code (e.g. '5710100')")
    name: str = Field(description="Course Name in English / Turkish")
    ects_credit: str = Field(default="", description="ECTS Credits")
    credit: str = Field(default="", description="METU Credits")
    level: str = Field(default="", description="Education Level (Lisans / Yüksek Lisans / Doktora)")
    type: str = Field(default="", description="Course Type / Status (e.g. Açık Ders)")


class ScheduleEntry(BaseModel):
    day: str = Field(default="", description="Day of week")
    time: str = Field(default="", description="Time slot")
    room: str = Field(default="", description="Classroom / Lab")


class CourseSection(BaseModel):
    section: str = Field(description="Section number (e.g. '1')")
    instructors: List[str] = Field(default_factory=list, description="Instructor names")
    syllabus_available: bool = Field(default=False, description="Whether syllabus button is available")
    critical_info: str = Field(default="", description="Critical course information / announcements")
    schedule: List[ScheduleEntry] = Field(default_factory=list, description="Schedule days and classrooms")


class SectionConstraint(BaseModel):
    """One row of a section's eligibility table.

    METU restricts a section by *department*: the table lists a row per
    programme admitted, and a student whose department has no row cannot
    register for that section at all. Within their row the surname must fall in
    ``[start_char, end_char]`` — two letters, not one, so "DJ"-"KA" admits KAYA
    but not KEMAL — and the CGPA and year of study in their own ranges.

    Every field is a string because the page prints them as text and an empty
    cell is meaningful ("not restricted"); coercing to numbers here would turn
    a blank into a 0.00 and silently exclude everybody.
    """

    given_dept: str = Field(default="", description="Abbreviation of the admitted department, e.g. 'CENG'")
    start_char: str = Field(default="", description="First surname prefix admitted, e.g. 'AA'")
    end_char: str = Field(default="", description="Last surname prefix admitted, e.g. 'ZZ'")
    min_cgpa: str = Field(default="", description="Minimum cumulative GPA")
    max_cgpa: str = Field(default="", description="Maximum cumulative GPA")
    min_year: str = Field(default="", description="Minimum year of study")
    max_year: str = Field(default="", description="Maximum year of study")
    start_grade: str = Field(default="", description="Lowest prior grade admitted, or 'Herkes alabilir'")
    end_grade: str = Field(default="", description="Highest prior grade admitted, or 'Herkes alabilir'")


class SectionConstraints(BaseModel):
    """The whole eligibility table for one section."""

    department: str = Field(default="")
    semester: str = Field(default="")
    course_code: str = Field(default="")
    section: str = Field(default="")
    constraints: List[SectionConstraint] = Field(default_factory=list)


class CourseDetails(BaseModel):
    department: str
    semester: str
    course_code: str
    course_name: str
    credit_info: str = Field(default="", description="Credit breakdown e.g. '4.00(3.00,2.00,0.00)'")
    sections: List[CourseSection] = Field(default_factory=list)


class CoursePrerequisite(BaseModel):
    program_code: str = Field(default="")
    dept_version: str = Field(default="")
    prerequisite_course_code: str = Field(description="Prerequisite Course Code")
    name: str = Field(description="Prerequisite Course Name")
    credit: str = Field(default="")
    set_no: str = Field(default="1", description="Prerequisite Set Group Number")
    min_grade: str = Field(default="DD", description="Minimum required letter grade (e.g. 'DD')")
    level_type: str = Field(default="")
    position: str = Field(default="", description="Status e.g. 'Offered Course / Açık Ders'")


class CourseReplacement(BaseModel):
    program_code: str = Field(default="")
    dept_version: str = Field(default="")
    replaced_course_code: str = Field(description="Equivalent / Replacement Course Code")
    name: str = Field(description="Course Name")
    credit: str = Field(default="")
    level: str = Field(default="")
    status: str = Field(default="", description="Status e.g. 'Kapali Ders' or 'Acik Ders'")


class ThesisCourse(BaseModel):
    course_code: str = Field(description="Course Code")
    name: str = Field(description="Course Name")
    ects_credit: str = Field(default="", description="ECTS Credits")
    credit: str = Field(default="", description="METU Credits")
    level: str = Field(default="", description="Level")
    type: str = Field(default="", description="Type")


class StudentProgramType(BaseModel):
    id: str = Field(description="Program Type ID (e.g. '1')")
    name: str = Field(description="Program Type Name (e.g. 'MAJOR')")


class StudentCourseCategory(BaseModel):
    id: str = Field(description="Category value for form (e.g. '1-236')")
    name: str = Field(description="Category Name (e.g. 'MUST COURSE', 'DEPARTMENTAL ELECTIVE')")


class StudentCategoryOverview(BaseModel):
    program_types: List[StudentProgramType]
    course_categories: List[StudentCourseCategory]


class StudentCategoryCourse(BaseModel):
    course_code: str = Field(description="Course Code")
    course_name: str = Field(description="Course Name")
    category: str = Field(description="Course Category")
    program_type: str = Field(description="Program Type")
    credit: str = Field(default="")
    year_or_ects: str = Field(default="")


class StudentCategoryResult(BaseModel):
    category_id: str
    category_name: str
    message: Optional[str] = None
    courses: List[StudentCategoryCourse] = Field(default_factory=list)
