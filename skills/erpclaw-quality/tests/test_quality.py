"""Tests for erpclaw-quality skill.

Covers: inspection templates, quality inspections, inspection readings,
non-conformance reports, quality goals, and dashboard.
"""
import json
import os
import sys

# Add scripts to path for importing db_query
SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

# Add shared lib
LIB_DIR = os.path.expanduser("~/.openclaw/erpclaw/lib")
if LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)

from db_query import ACTIONS  # noqa: E402
from helpers import (  # noqa: E402
    _call_action,
    create_test_company,
    create_test_item,
    create_test_inspection_template,
    create_test_quality_inspection,
    create_test_non_conformance,
    create_test_quality_goal,
    setup_quality_environment,
)


# ===========================================================================
# Inspection Template Tests
# ===========================================================================

class TestInspectionTemplates:
    """Tests for inspection template CRUD."""

    def test_add_inspection_template(self, fresh_db):
        """Create a template with both numeric and non-numeric parameters."""
        env = setup_quality_environment(fresh_db)

        parameters = [
            {
                "parameter_name": "Length",
                "parameter_type": "numeric",
                "min_value": "9.5",
                "max_value": "10.5",
                "uom": "cm",
            },
            {
                "parameter_name": "Color Check",
                "parameter_type": "non_numeric",
                "acceptance_value": "Blue",
            },
        ]

        result = _call_action(
            ACTIONS["add-inspection-template"], fresh_db,
            name="Widget QC Template",
            inspection_type="incoming",
            item_id=env["item_id"],
            description="Standard quality check for widgets",
            parameters=json.dumps(parameters),
        )

        assert result["status"] == "ok"
        tmpl = result["template"]
        assert tmpl["name"] == "Widget QC Template"
        assert tmpl["inspection_type"] == "incoming"
        assert tmpl["item_id"] == env["item_id"]
        assert tmpl["description"] == "Standard quality check for widgets"
        assert len(tmpl["parameters"]) == 2

        # Verify numeric parameter
        p0 = tmpl["parameters"][0]
        assert p0["parameter_name"] == "Length"
        assert p0["parameter_type"] == "numeric"
        assert p0["min_value"] == "9.5"
        assert p0["max_value"] == "10.5"
        assert p0["uom"] == "cm"

        # Verify non-numeric parameter
        p1 = tmpl["parameters"][1]
        assert p1["parameter_name"] == "Color Check"
        assert p1["parameter_type"] == "non_numeric"
        assert p1["acceptance_value"] == "Blue"

    def test_get_inspection_template(self, fresh_db):
        """Verify get-inspection-template returns template with all parameters."""
        env = setup_quality_environment(fresh_db)

        parameters = [
            {
                "parameter_name": "Weight",
                "parameter_type": "numeric",
                "min_value": "4.5",
                "max_value": "5.5",
                "uom": "kg",
            },
            {
                "parameter_name": "Surface Finish",
                "parameter_type": "non_numeric",
                "acceptance_value": "Smooth",
            },
        ]

        template_id = create_test_inspection_template(
            fresh_db, name="Weight Check Template",
            inspection_type="outgoing",
            item_id=env["item_id"],
            parameters=parameters,
        )

        result = _call_action(
            ACTIONS["get-inspection-template"], fresh_db,
            template_id=template_id,
        )

        assert result["status"] == "ok"
        tmpl = result["template"]
        assert tmpl["id"] == template_id
        assert tmpl["name"] == "Weight Check Template"
        assert tmpl["inspection_type"] == "outgoing"
        assert tmpl["item_id"] == env["item_id"]
        assert len(tmpl["parameters"]) == 2

        # Parameters should be sorted by sort_order
        assert tmpl["parameters"][0]["parameter_name"] == "Weight"
        assert tmpl["parameters"][1]["parameter_name"] == "Surface Finish"

    def test_list_inspection_templates(self, fresh_db):
        """List templates with filters by type and search."""
        env = setup_quality_environment(fresh_db)

        # Create templates of different types
        create_test_inspection_template(
            fresh_db, name="Incoming Widget Check",
            inspection_type="incoming", item_id=env["item_id"],
        )
        create_test_inspection_template(
            fresh_db, name="Outgoing Widget Check",
            inspection_type="outgoing", item_id=env["item_id"],
        )
        create_test_inspection_template(
            fresh_db, name="In-Process Widget Check",
            inspection_type="in_process", item_id=env["item_id_2"],
        )

        # List all
        result = _call_action(ACTIONS["list-inspection-templates"], fresh_db)
        assert result["status"] == "ok"
        assert result["total"] == 3

        # Filter by type
        result = _call_action(
            ACTIONS["list-inspection-templates"], fresh_db,
            inspection_type="incoming",
        )
        assert result["status"] == "ok"
        assert result["total"] == 1
        assert result["templates"][0]["name"] == "Incoming Widget Check"

        # Search by name
        result = _call_action(
            ACTIONS["list-inspection-templates"], fresh_db,
            search="Outgoing",
        )
        assert result["status"] == "ok"
        assert result["total"] == 1
        assert result["templates"][0]["name"] == "Outgoing Widget Check"


# ===========================================================================
# Quality Inspection Tests
# ===========================================================================

class TestQualityInspections:
    """Tests for quality inspection lifecycle."""

    def test_add_quality_inspection(self, fresh_db):
        """Create a quality inspection without a template."""
        env = setup_quality_environment(fresh_db)

        result = _call_action(
            ACTIONS["add-quality-inspection"], fresh_db,
            item_id=env["item_id"],
            inspection_type="incoming",
            inspection_date="2026-02-15",
            inspected_by="John Smith",
            sample_size="5",
            remarks="Spot check on incoming batch",
        )

        assert result["status"] == "ok"
        insp = result["inspection"]
        assert insp["item_id"] == env["item_id"]
        assert insp["inspection_type"] == "incoming"
        assert insp["inspection_date"] == "2026-02-15"
        assert insp["inspected_by"] == "John Smith"
        assert insp["sample_size"] == 5
        assert insp["status"] == "accepted"
        assert insp["remarks"] == "Spot check on incoming batch"
        assert insp["readings"] == []
        assert insp["naming_series"].startswith("QC-")

    def test_add_quality_inspection_from_template(self, fresh_db):
        """Create inspection from template; reading rows auto-created."""
        env = setup_quality_environment(fresh_db)

        parameters = [
            {
                "parameter_name": "Diameter",
                "parameter_type": "numeric",
                "min_value": "9.8",
                "max_value": "10.2",
                "uom": "mm",
            },
            {
                "parameter_name": "Visual Check",
                "parameter_type": "non_numeric",
                "acceptance_value": "Pass",
            },
        ]

        template_id = create_test_inspection_template(
            fresh_db, name="Diameter Template",
            inspection_type="incoming",
            item_id=env["item_id"],
            parameters=parameters,
        )

        result = _call_action(
            ACTIONS["add-quality-inspection"], fresh_db,
            item_id=env["item_id"],
            inspection_type="incoming",
            inspection_date="2026-02-15",
            template_id=template_id,
        )

        assert result["status"] == "ok"
        insp = result["inspection"]
        assert insp["template_id"] == template_id
        assert len(insp["readings"]) == 2

        # Readings are created with status 'accepted' and no value yet
        r0 = insp["readings"][0]
        assert r0["parameter_name"] == "Diameter"
        assert r0["reading_value"] is None
        assert r0["status"] == "accepted"

        r1 = insp["readings"][1]
        assert r1["parameter_name"] == "Visual Check"
        assert r1["reading_value"] is None
        assert r1["status"] == "accepted"

    def test_record_inspection_readings_numeric_pass(self, fresh_db):
        """Record a numeric reading within min/max range -> accepted."""
        env = setup_quality_environment(fresh_db)

        parameters = [
            {
                "parameter_name": "Thickness",
                "parameter_type": "numeric",
                "min_value": "2.0",
                "max_value": "3.0",
                "uom": "mm",
            },
        ]

        template_id = create_test_inspection_template(
            fresh_db, name="Thickness Check",
            inspection_type="incoming",
            parameters=parameters,
        )

        insp_result = create_test_quality_inspection(
            fresh_db, item_id=env["item_id"],
            inspection_type="incoming",
            template_id=template_id,
        )
        inspection_id = insp_result["inspection"]["id"]
        param_id = insp_result["inspection"]["readings"][0]["parameter_id"]

        # Record reading within range
        readings_json = json.dumps([
            {"parameter_id": param_id, "reading_value": "2.5"},
        ])
        result = _call_action(
            ACTIONS["record-inspection-readings"], fresh_db,
            quality_inspection_id=inspection_id,
            readings=readings_json,
        )

        assert result["status"] == "ok"
        assert len(result["readings"]) == 1
        assert result["readings"][0]["reading_value"] == "2.5"
        assert result["readings"][0]["status"] == "accepted"

    def test_record_inspection_readings_numeric_fail(self, fresh_db):
        """Record a numeric reading outside min/max range -> rejected."""
        env = setup_quality_environment(fresh_db)

        parameters = [
            {
                "parameter_name": "Thickness",
                "parameter_type": "numeric",
                "min_value": "2.0",
                "max_value": "3.0",
                "uom": "mm",
            },
        ]

        template_id = create_test_inspection_template(
            fresh_db, name="Thickness Check Fail",
            inspection_type="incoming",
            parameters=parameters,
        )

        insp_result = create_test_quality_inspection(
            fresh_db, item_id=env["item_id"],
            inspection_type="incoming",
            template_id=template_id,
        )
        inspection_id = insp_result["inspection"]["id"]
        param_id = insp_result["inspection"]["readings"][0]["parameter_id"]

        # Record reading ABOVE max_value
        readings_json = json.dumps([
            {"parameter_id": param_id, "reading_value": "3.5"},
        ])
        result = _call_action(
            ACTIONS["record-inspection-readings"], fresh_db,
            quality_inspection_id=inspection_id,
            readings=readings_json,
        )

        assert result["status"] == "ok"
        assert result["readings"][0]["reading_value"] == "3.5"
        assert result["readings"][0]["status"] == "rejected"

    def test_record_inspection_readings_numeric_below_min(self, fresh_db):
        """Record a numeric reading below min_value -> rejected."""
        env = setup_quality_environment(fresh_db)

        parameters = [
            {
                "parameter_name": "Temperature",
                "parameter_type": "numeric",
                "min_value": "20.0",
                "max_value": "30.0",
                "uom": "C",
            },
        ]

        template_id = create_test_inspection_template(
            fresh_db, name="Temp Check Below",
            inspection_type="in_process",
            parameters=parameters,
        )

        insp_result = create_test_quality_inspection(
            fresh_db, item_id=env["item_id"],
            inspection_type="in_process",
            template_id=template_id,
        )
        inspection_id = insp_result["inspection"]["id"]
        param_id = insp_result["inspection"]["readings"][0]["parameter_id"]

        # Record reading BELOW min_value
        readings_json = json.dumps([
            {"parameter_id": param_id, "reading_value": "18.5"},
        ])
        result = _call_action(
            ACTIONS["record-inspection-readings"], fresh_db,
            quality_inspection_id=inspection_id,
            readings=readings_json,
        )

        assert result["status"] == "ok"
        assert result["readings"][0]["status"] == "rejected"

    def test_record_inspection_readings_non_numeric(self, fresh_db):
        """Record a non-numeric reading matching acceptance_value -> accepted."""
        env = setup_quality_environment(fresh_db)

        parameters = [
            {
                "parameter_name": "Color",
                "parameter_type": "non_numeric",
                "acceptance_value": "Red",
            },
        ]

        template_id = create_test_inspection_template(
            fresh_db, name="Color Check",
            inspection_type="outgoing",
            parameters=parameters,
        )

        insp_result = create_test_quality_inspection(
            fresh_db, item_id=env["item_id"],
            inspection_type="outgoing",
            template_id=template_id,
        )
        inspection_id = insp_result["inspection"]["id"]
        param_id = insp_result["inspection"]["readings"][0]["parameter_id"]

        # Matching acceptance value
        readings_json = json.dumps([
            {"parameter_id": param_id, "reading_value": "Red"},
        ])
        result = _call_action(
            ACTIONS["record-inspection-readings"], fresh_db,
            quality_inspection_id=inspection_id,
            readings=readings_json,
        )

        assert result["status"] == "ok"
        assert result["readings"][0]["reading_value"] == "Red"
        assert result["readings"][0]["status"] == "accepted"

        # Now test non-matching value
        readings_json = json.dumps([
            {"parameter_id": param_id, "reading_value": "Green"},
        ])
        result = _call_action(
            ACTIONS["record-inspection-readings"], fresh_db,
            quality_inspection_id=inspection_id,
            readings=readings_json,
        )

        assert result["status"] == "ok"
        assert result["readings"][0]["reading_value"] == "Green"
        assert result["readings"][0]["status"] == "rejected"

    def test_evaluate_inspection_all_accepted(self, fresh_db):
        """All readings accepted -> inspection status = accepted."""
        env = setup_quality_environment(fresh_db)

        parameters = [
            {
                "parameter_name": "Length",
                "parameter_type": "numeric",
                "min_value": "9.0",
                "max_value": "11.0",
            },
            {
                "parameter_name": "Width",
                "parameter_type": "numeric",
                "min_value": "4.0",
                "max_value": "6.0",
            },
        ]

        template_id = create_test_inspection_template(
            fresh_db, name="Dimension Check All Pass",
            inspection_type="incoming",
            parameters=parameters,
        )

        insp_result = create_test_quality_inspection(
            fresh_db, item_id=env["item_id"],
            inspection_type="incoming",
            template_id=template_id,
        )
        inspection_id = insp_result["inspection"]["id"]
        readings = insp_result["inspection"]["readings"]

        # Record all values within range
        readings_json = json.dumps([
            {"parameter_id": readings[0]["parameter_id"], "reading_value": "10.0"},
            {"parameter_id": readings[1]["parameter_id"], "reading_value": "5.0"},
        ])
        _call_action(
            ACTIONS["record-inspection-readings"], fresh_db,
            quality_inspection_id=inspection_id,
            readings=readings_json,
        )

        # Evaluate
        result = _call_action(
            ACTIONS["evaluate-inspection"], fresh_db,
            quality_inspection_id=inspection_id,
        )

        assert result["status"] == "ok"
        assert result["new_status"] == "accepted"
        assert result["total_readings"] == 2
        assert result["accepted_count"] == 2
        assert result["rejected_count"] == 0

    def test_evaluate_inspection_mixed(self, fresh_db):
        """Some readings pass, some fail -> partially_accepted."""
        env = setup_quality_environment(fresh_db)

        parameters = [
            {
                "parameter_name": "Length",
                "parameter_type": "numeric",
                "min_value": "9.0",
                "max_value": "11.0",
            },
            {
                "parameter_name": "Width",
                "parameter_type": "numeric",
                "min_value": "4.0",
                "max_value": "6.0",
            },
        ]

        template_id = create_test_inspection_template(
            fresh_db, name="Dimension Check Mixed",
            inspection_type="incoming",
            parameters=parameters,
        )

        insp_result = create_test_quality_inspection(
            fresh_db, item_id=env["item_id"],
            inspection_type="incoming",
            template_id=template_id,
        )
        inspection_id = insp_result["inspection"]["id"]
        readings = insp_result["inspection"]["readings"]

        # Record: Length within range, Width outside range
        readings_json = json.dumps([
            {"parameter_id": readings[0]["parameter_id"], "reading_value": "10.0"},
            {"parameter_id": readings[1]["parameter_id"], "reading_value": "7.5"},
        ])
        _call_action(
            ACTIONS["record-inspection-readings"], fresh_db,
            quality_inspection_id=inspection_id,
            readings=readings_json,
        )

        # Evaluate
        result = _call_action(
            ACTIONS["evaluate-inspection"], fresh_db,
            quality_inspection_id=inspection_id,
        )

        assert result["status"] == "ok"
        assert result["new_status"] == "partially_accepted"
        assert result["total_readings"] == 2
        assert result["accepted_count"] == 1
        assert result["rejected_count"] == 1

    def test_evaluate_inspection_all_rejected(self, fresh_db):
        """All readings fail -> rejected."""
        env = setup_quality_environment(fresh_db)

        parameters = [
            {
                "parameter_name": "Length",
                "parameter_type": "numeric",
                "min_value": "9.0",
                "max_value": "11.0",
            },
            {
                "parameter_name": "Color",
                "parameter_type": "non_numeric",
                "acceptance_value": "Blue",
            },
        ]

        template_id = create_test_inspection_template(
            fresh_db, name="All Fail Template",
            inspection_type="incoming",
            parameters=parameters,
        )

        insp_result = create_test_quality_inspection(
            fresh_db, item_id=env["item_id"],
            inspection_type="incoming",
            template_id=template_id,
        )
        inspection_id = insp_result["inspection"]["id"]
        readings = insp_result["inspection"]["readings"]

        # Record: Length out of range, Color mismatch
        readings_json = json.dumps([
            {"parameter_id": readings[0]["parameter_id"], "reading_value": "15.0"},
            {"parameter_id": readings[1]["parameter_id"], "reading_value": "Red"},
        ])
        _call_action(
            ACTIONS["record-inspection-readings"], fresh_db,
            quality_inspection_id=inspection_id,
            readings=readings_json,
        )

        # Evaluate
        result = _call_action(
            ACTIONS["evaluate-inspection"], fresh_db,
            quality_inspection_id=inspection_id,
        )

        assert result["status"] == "ok"
        assert result["new_status"] == "rejected"
        assert result["total_readings"] == 2
        assert result["accepted_count"] == 0
        assert result["rejected_count"] == 2

    def test_list_quality_inspections(self, fresh_db):
        """List inspections with status and type filters."""
        env = setup_quality_environment(fresh_db)

        # Create two inspections of different types
        create_test_quality_inspection(
            fresh_db, item_id=env["item_id"],
            inspection_type="incoming",
            inspection_date="2026-02-10",
        )
        create_test_quality_inspection(
            fresh_db, item_id=env["item_id"],
            inspection_type="outgoing",
            inspection_date="2026-02-12",
        )

        # List all
        result = _call_action(ACTIONS["list-quality-inspections"], fresh_db)
        assert result["status"] == "ok"
        assert result["total"] == 2

        # Filter by type
        result = _call_action(
            ACTIONS["list-quality-inspections"], fresh_db,
            inspection_type="incoming",
        )
        assert result["status"] == "ok"
        assert result["total"] == 1
        assert result["inspections"][0]["inspection_type"] == "incoming"

        # Filter by status (all default to 'accepted')
        result = _call_action(
            ACTIONS["list-quality-inspections"], fresh_db,
            status="accepted",
        )
        assert result["status"] == "ok"
        assert result["total"] == 2

        # Filter by status that yields zero results
        result = _call_action(
            ACTIONS["list-quality-inspections"], fresh_db,
            status="rejected",
        )
        assert result["status"] == "ok"
        assert result["total"] == 0


# ===========================================================================
# Non-Conformance Tests
# ===========================================================================

class TestNonConformance:
    """Tests for non-conformance report CRUD."""

    def test_add_non_conformance(self, fresh_db):
        """Create an NCR with severity."""
        env = setup_quality_environment(fresh_db)

        result = _call_action(
            ACTIONS["add-non-conformance"], fresh_db,
            description="Dent found on housing panel",
            severity="major",
            item_id=env["item_id"],
            root_cause="Drop during transport",
            corrective_action="Replace unit",
        )

        assert result["status"] == "ok"
        nc = result["non_conformance"]
        assert nc["description"] == "Dent found on housing panel"
        assert nc["severity"] == "major"
        assert nc["item_id"] == env["item_id"]
        assert nc["root_cause"] == "Drop during transport"
        assert nc["corrective_action"] == "Replace unit"
        assert nc["status"] == "open"
        assert nc["naming_series"].startswith("NC-")

    def test_update_non_conformance(self, fresh_db):
        """Update root_cause and corrective_action on an NCR."""
        env = setup_quality_environment(fresh_db)

        nc_result = create_test_non_conformance(
            fresh_db, description="Surface scratch",
            severity="minor", item_id=env["item_id"],
        )
        nc_id = nc_result["non_conformance"]["id"]

        result = _call_action(
            ACTIONS["update-non-conformance"], fresh_db,
            non_conformance_id=nc_id,
            root_cause="Improper handling during packaging",
            corrective_action="Add protective sleeve",
            preventive_action="Train packaging staff",
        )

        assert result["status"] == "ok"
        nc = result["non_conformance"]
        assert nc["root_cause"] == "Improper handling during packaging"
        assert nc["corrective_action"] == "Add protective sleeve"
        assert nc["preventive_action"] == "Train packaging staff"
        assert nc["status"] == "open"  # unchanged

    def test_update_non_conformance_close(self, fresh_db):
        """Close an NCR; requires resolution_date."""
        env = setup_quality_environment(fresh_db)

        nc_result = create_test_non_conformance(
            fresh_db, description="Dimension out of spec",
            severity="critical", item_id=env["item_id"],
        )
        nc_id = nc_result["non_conformance"]["id"]

        result = _call_action(
            ACTIONS["update-non-conformance"], fresh_db,
            non_conformance_id=nc_id,
            status="closed",
            resolution_date="2026-02-16",
            root_cause="Worn tooling",
            corrective_action="Replace tooling",
        )

        assert result["status"] == "ok"
        nc = result["non_conformance"]
        assert nc["status"] == "closed"
        assert nc["resolution_date"] == "2026-02-16"

    def test_update_non_conformance_close_without_date(self, fresh_db):
        """Close without resolution_date should error."""
        env = setup_quality_environment(fresh_db)

        nc_result = create_test_non_conformance(
            fresh_db, description="Missing label",
            severity="minor", item_id=env["item_id"],
        )
        nc_id = nc_result["non_conformance"]["id"]

        result = _call_action(
            ACTIONS["update-non-conformance"], fresh_db,
            non_conformance_id=nc_id,
            status="closed",
        )

        assert result["status"] == "error"
        assert "resolution-date" in result["message"].lower() or \
               "resolution_date" in result["message"].lower()

    def test_list_non_conformances(self, fresh_db):
        """List NCRs with severity and status filters."""
        env = setup_quality_environment(fresh_db)

        create_test_non_conformance(
            fresh_db, description="Minor scratch",
            severity="minor", item_id=env["item_id"],
        )
        create_test_non_conformance(
            fresh_db, description="Major dent",
            severity="major", item_id=env["item_id"],
        )
        create_test_non_conformance(
            fresh_db, description="Critical crack",
            severity="critical", item_id=env["item_id_2"],
        )

        # List all
        result = _call_action(ACTIONS["list-non-conformances"], fresh_db)
        assert result["status"] == "ok"
        assert result["total"] == 3

        # Filter by severity
        result = _call_action(
            ACTIONS["list-non-conformances"], fresh_db,
            severity="critical",
        )
        assert result["status"] == "ok"
        assert result["total"] == 1
        assert result["non_conformances"][0]["description"] == "Critical crack"

        # Filter by status (all should be 'open')
        result = _call_action(
            ACTIONS["list-non-conformances"], fresh_db,
            status="open",
        )
        assert result["status"] == "ok"
        assert result["total"] == 3


# ===========================================================================
# Quality Goal Tests
# ===========================================================================

class TestQualityGoals:
    """Tests for quality goal CRUD."""

    def test_add_quality_goal(self, fresh_db):
        """Create a quality goal with target_value."""
        # Quality goals do not require a company — no naming series used
        # But setup_quality_environment gives us a consistent env
        setup_quality_environment(fresh_db)

        result = _call_action(
            ACTIONS["add-quality-goal"], fresh_db,
            name="Defect Rate Below 1%",
            target_value="1.00",
            measurable="Percentage of defective items per batch",
            monitoring_frequency="weekly",
            review_date="2026-03-01",
        )

        assert result["status"] == "ok"
        goal = result["quality_goal"]
        assert goal["name"] == "Defect Rate Below 1%"
        assert goal["target_value"] == "1.00"
        assert goal["current_value"] == "0"
        assert goal["status"] == "on_track"
        assert goal["monitoring_frequency"] == "weekly"
        assert goal["measurable"] == "Percentage of defective items per batch"
        assert goal["review_date"] == "2026-03-01"

    def test_update_quality_goal(self, fresh_db):
        """Update current_value and status on a quality goal."""
        setup_quality_environment(fresh_db)

        goal_result = create_test_quality_goal(
            fresh_db, name="On-Time Delivery Rate",
            target_value="95.00",
            monitoring_frequency="monthly",
        )
        goal_id = goal_result["quality_goal"]["id"]

        # Update current value and mark as at_risk
        result = _call_action(
            ACTIONS["update-quality-goal"], fresh_db,
            quality_goal_id=goal_id,
            current_value="88.50",
            status="at_risk",
        )

        assert result["status"] == "ok"
        goal = result["quality_goal"]
        assert goal["current_value"] == "88.50"
        assert goal["status"] == "at_risk"
        assert goal["target_value"] == "95.00"  # unchanged


# ===========================================================================
# Dashboard Tests
# ===========================================================================

class TestDashboard:
    """Tests for the quality dashboard and status alias."""

    def test_quality_dashboard(self, fresh_db):
        """Verify aggregated stats in the quality dashboard."""
        env = setup_quality_environment(fresh_db)

        # Create 3 inspections, force different statuses via evaluate
        params = [
            {
                "parameter_name": "Check A",
                "parameter_type": "numeric",
                "min_value": "1.0",
                "max_value": "10.0",
            },
        ]
        template_id = create_test_inspection_template(
            fresh_db, name="Dashboard Template",
            inspection_type="incoming",
            parameters=params,
        )

        # Inspection 1: will remain accepted (value in range)
        insp1 = create_test_quality_inspection(
            fresh_db, item_id=env["item_id"],
            inspection_type="incoming",
            template_id=template_id,
        )
        insp1_id = insp1["inspection"]["id"]
        insp1_param = insp1["inspection"]["readings"][0]["parameter_id"]
        _call_action(
            ACTIONS["record-inspection-readings"], fresh_db,
            quality_inspection_id=insp1_id,
            readings=json.dumps([{"parameter_id": insp1_param, "reading_value": "5.0"}]),
        )
        _call_action(
            ACTIONS["evaluate-inspection"], fresh_db,
            quality_inspection_id=insp1_id,
        )

        # Inspection 2: will be rejected (value out of range)
        insp2 = create_test_quality_inspection(
            fresh_db, item_id=env["item_id"],
            inspection_type="incoming",
            template_id=template_id,
        )
        insp2_id = insp2["inspection"]["id"]
        insp2_param = insp2["inspection"]["readings"][0]["parameter_id"]
        _call_action(
            ACTIONS["record-inspection-readings"], fresh_db,
            quality_inspection_id=insp2_id,
            readings=json.dumps([{"parameter_id": insp2_param, "reading_value": "15.0"}]),
        )
        _call_action(
            ACTIONS["evaluate-inspection"], fresh_db,
            quality_inspection_id=insp2_id,
        )

        # Inspection 3: also accepted
        insp3 = create_test_quality_inspection(
            fresh_db, item_id=env["item_id"],
            inspection_type="outgoing",
            template_id=template_id,
        )
        insp3_id = insp3["inspection"]["id"]
        insp3_param = insp3["inspection"]["readings"][0]["parameter_id"]
        _call_action(
            ACTIONS["record-inspection-readings"], fresh_db,
            quality_inspection_id=insp3_id,
            readings=json.dumps([{"parameter_id": insp3_param, "reading_value": "8.0"}]),
        )
        _call_action(
            ACTIONS["evaluate-inspection"], fresh_db,
            quality_inspection_id=insp3_id,
        )

        # Create NCRs: 1 minor (open), 1 critical (open)
        create_test_non_conformance(
            fresh_db, description="Minor scratch",
            severity="minor", item_id=env["item_id"],
        )
        create_test_non_conformance(
            fresh_db, description="Critical failure",
            severity="critical", item_id=env["item_id"],
        )

        # Create quality goals: 1 on_track, 1 at_risk
        create_test_quality_goal(
            fresh_db, name="Goal A", target_value="95.00",
        )
        goal_b_result = create_test_quality_goal(
            fresh_db, name="Goal B", target_value="99.00",
        )
        _call_action(
            ACTIONS["update-quality-goal"], fresh_db,
            quality_goal_id=goal_b_result["quality_goal"]["id"],
            status="at_risk",
        )

        # Now check dashboard
        result = _call_action(ACTIONS["quality-dashboard"], fresh_db)
        assert result["status"] == "ok"
        dashboard = result["dashboard"]

        # Inspections
        assert dashboard["inspections"]["total"] == 3
        assert dashboard["inspections"]["by_status"]["accepted"] == 2
        assert dashboard["inspections"]["by_status"]["rejected"] == 1
        # Pass rate: 2/3 = 66.67%
        assert dashboard["inspections"]["pass_rate_pct"] == "66.67"

        # Non-conformances (all open)
        assert dashboard["non_conformances"]["total_open"] == 2
        assert dashboard["non_conformances"]["by_severity"]["minor"] == 1
        assert dashboard["non_conformances"]["by_severity"]["critical"] == 1

        # Quality goals
        assert dashboard["quality_goals"]["total"] == 2
        assert dashboard["quality_goals"]["by_status"]["on_track"] == 1
        assert dashboard["quality_goals"]["by_status"]["at_risk"] == 1

    def test_status(self, fresh_db):
        """Status action is an alias for quality-dashboard."""
        setup_quality_environment(fresh_db)

        result = _call_action(ACTIONS["status"], fresh_db)
        assert result["status"] == "ok"
        assert "dashboard" in result
        # Empty DB should have zero totals
        assert result["dashboard"]["inspections"]["total"] == 0
        assert result["dashboard"]["non_conformances"]["total_open"] == 0
        assert result["dashboard"]["quality_goals"]["total"] == 0
