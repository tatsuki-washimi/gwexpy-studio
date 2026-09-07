"""Real parameter panel controls retain inputs and validate requests."""

import json

import pytest


def _native_catalog(*, direction: str = "read") -> dict[str, object]:
    """Return the unchanged development-mode shape produced by native I/O."""
    return {
        "datatype": "TimeSeries",
        "direction": direction,
        "formats": [
            {
                "format": "hdf5",
                "read": True,
                "write": True,
                "auto_identify": True,
            },
            {
                "format": "write-only",
                "read": False,
                "write": True,
                "auto_identify": False,
            },
        ],
    }


def _frozen_catalog(*, direction: str = "read") -> dict[str, object]:
    """Return a direction-level artifact policy with all three release tiers."""
    return {
        **_native_catalog(direction=direction),
        "capability_mode": "frozen",
        "capability_digest": "a" * 64,
        "formats": [
            {
                "format": "hdf5",
                "read": True,
                "write": True,
                "auto_identify": True,
                "capabilities": {
                    "read": {
                        "available": True,
                        "tier": "A",
                        "caveat": None,
                        "reason": None,
                    },
                    "write": {
                        "available": True,
                        "tier": "A",
                        "caveat": None,
                        "reason": None,
                    },
                },
            },
            {
                "format": "csv",
                "read": True,
                "write": True,
                "auto_identify": False,
                "capabilities": {
                    "read": {
                        "available": True,
                        "tier": "B",
                        "caveat": "Names and channels may be lost",
                        "reason": None,
                    },
                    "write": {
                        "available": True,
                        "tier": "B",
                        "caveat": "Names and channels may be lost",
                        "reason": None,
                    },
                },
            },
            {
                "format": "mseed",
                "read": True,
                "write": True,
                "auto_identify": False,
                "capabilities": {
                    "read": {
                        "available": False,
                        "tier": "C",
                        "caveat": None,
                        "reason": "backend_not_bundled",
                    },
                    "write": {
                        "available": False,
                        "tier": "C",
                        "caveat": None,
                        "reason": "backend_not_bundled",
                    },
                },
            },
        ],
    }


def _format_items(panel: object) -> list[str]:
    """Return the visible entries of one data-I/O format combobox."""
    return [
        panel.format_combo.itemText(index)
        for index in range(panel.format_combo.count())
    ]


@pytest.mark.contract(id="GUI-SIG-PAN-001")
@pytest.mark.gui
def test_parameter_panel_roles_swap_scalar_and_filter_preview(qapp):
    """Convert real typed fields and preserve failed preview values."""
    import gwexpy_studio.ui.parameter_panel as panels

    assert hasattr(panels, "ParameterPanel"), "shared parameter panel is required"
    panel = panels.ParameterPanel()
    panel.set_objects([("A", {"object_id": "a"}), ("B", {"object_id": "b"})])
    panel.set_operation("timeseries.csd")
    panel.other_combo.setCurrentIndex(1)
    panel.swap_button.click()
    request = panel.request()
    assert request["inputs"]["self"] == {"object_id": "b"}
    assert request["inputs"]["other"] == {"object_id": "a"}
    panel.set_operation("arithmetic")
    panel.fields["operand_mode"].setCurrentText("scalar")
    panel.fields["scalar"].setText("2.5")
    panel.fields["unit"].setText("m")
    panel.fields["reverse"].setChecked(True)
    assert panel.request()["params"]["scalar"] == {"value": 2.5, "unit": "m"}
    assert "other" not in panel.request()["inputs"]
    panel.set_operation("timeseries.lowpass")
    panel.fields["frequency"].setText("20")
    seen = []
    panel.preview_requested.connect(seen.append)
    panel.preview_button.click()
    assert seen[0]["params"]["filtfilt"] is True
    assert seen[0]["generation"] == panel.generation
    assert not panel.preview_button.isEnabled()
    panel.show_error("axes_mismatch", "Sample times differ")
    assert panel.fields["frequency"].text() == "20"
    assert panel.preview_button.isEnabled()
    panel.close()


@pytest.mark.contract(id="GUI-SIG-PAN-002")
@pytest.mark.gui
def test_io_panel_supports_all_kinds_custom_format_and_typed_json(qapp):
    """Expose native classes, free formats, JSON values, and resource limits."""
    import gwexpy_studio.ui.io_panel as panels

    assert hasattr(panels, "DataIOPanel"), "native I/O configuration is required"
    panel = panels.DataIOPanel(direction="read")
    assert panel.datatype_combo.count() == 12
    panel.datatype_combo.setCurrentText("FrequencySeriesMatrix")
    panel.format_combo.setEditText("custom-reader")
    panel.paths_edit.setPlainText("/tmp/a.dat\n/tmp/b.dat")
    panel.args_edit.setPlainText('[{"__type__":"tuple","items":[1,2]}]')
    panel.kwargs_edit.setPlainText(
        '{"scale":{"__type__":"quantity","value":2,"unit":"m"}}'
    )
    request = panel.request()
    assert request["datatype"] == "FrequencySeriesMatrix"
    assert request["format"] == "custom-reader"
    assert request["paths"] == ["/tmp/a.dat", "/tmp/b.dat"]
    assert request["max_bytes"] == 512 * 1024 * 1024
    assert request["max_entries"] == 10_000
    assert request["args"] == json.loads(panel.args_edit.toPlainText())
    panel.kwargs_edit.setPlainText('{"danger": NaN}')
    with pytest.raises(ValueError, match="finite|NaN"):
        panel.request()
    panel.close()


@pytest.mark.contract(id="GUI-SIG-PAN-003")
@pytest.mark.gui
def test_io_confirmation_is_invalidated_by_edits_and_failure_keeps_request(qapp):
    """Require another inspection after changing the confirmed request."""
    from gwexpy_studio.ui.io_panel import DataIOPanel

    panel = DataIOPanel(direction="read")
    panel.paths_edit.setPlainText("/tmp/input.csv")
    inspected = []
    loaded = []
    panel.inspect_requested.connect(inspected.append)
    panel.read_requested.connect(loaded.append)
    panel.inspect_button.click()
    assert not panel.datatype_combo.isEnabled()
    request = inspected[0]["request"]
    panel.show_inspection({"request": request, "total_bytes": 100, "entry_count": 1})
    assert panel.confirm_button.isEnabled()
    assert panel.datatype_combo.isEnabled()
    panel.kwargs_edit.setPlainText('{"header":1}')
    assert not panel.confirm_button.isEnabled()
    assert not loaded
    panel.inspect_button.click()
    panel.show_error("missing_dependency", "Install the native reader dependency")
    assert panel.kwargs_edit.toPlainText() == '{"header":1}'
    assert panel.inspect_button.isEnabled()
    panel.close()


@pytest.mark.contract(id="GUI-SIG-PAN-004")
@pytest.mark.gui
def test_changed_filter_form_rejects_stale_response(qapp):
    """Ignore a completed preview when its request parameters changed."""
    from gwexpy_studio.ui.parameter_panel import ParameterPanel

    panel = ParameterPanel()
    panel.set_objects([("A", {"object_id": "a"})])
    panel.set_operation("timeseries.lowpass")
    panel.fields["frequency"].setText("20")
    panel.preview_button.click()
    generation = panel.generation
    panel.fields["frequency"].setText("30")
    assert panel.generation > generation
    assert panel.show_responses((), generation) is False
    assert panel.response_canvas.isHidden()
    assert panel.preview_button.isEnabled()
    panel.close()


@pytest.mark.contract(id="GUI-SIG-PAN-005")
@pytest.mark.gui
def test_large_byte_limit_and_overflowing_json_numbers(qapp):
    """Allow explicit large limits while rejecting numeric JSON overflow."""
    from gwexpy_studio.ui.io_panel import DataIOPanel

    panel = DataIOPanel()
    panel.paths_edit.setPlainText("/tmp/input.dat")
    panel.max_bytes_spin.setValue(8 * 1024**3)
    assert panel.request()["max_bytes"] == 8 * 1024**3
    assert isinstance(panel.request()["max_bytes"], int)
    panel.kwargs_edit.setPlainText('{"scale":1e309}')
    with pytest.raises(ValueError, match="finite"):
        panel.request()
    panel.close()


@pytest.mark.contract(id="GUI-SIG-PAN-006")
@pytest.mark.gui
def test_inspection_completion_with_invalid_edits_is_an_inline_error(qapp):
    """Discard stale results when the editable request is temporarily invalid."""
    from gwexpy_studio.ui.io_panel import DataIOPanel

    panel = DataIOPanel()
    panel.paths_edit.setPlainText("/tmp/input.dat")
    inspection = {"request": panel.request()}
    panel.inspect_button.click()
    panel.paths_edit.clear()
    panel.show_inspection(inspection)
    assert "stale_inspection" in panel.error_label.text()
    assert not panel.confirm_button.isEnabled()
    assert panel.inspect_button.isEnabled()
    panel.close()


@pytest.mark.contract(id="GUI-SIG-PAN-007")
@pytest.mark.gui
def test_recorded_filter_preview_uses_recipe_until_parameters_change(qapp):
    """Request recorded coefficients only for the unchanged reopened recipe."""
    from gwexpy_studio.ui.parameter_panel import ParameterPanel

    panel = ParameterPanel()
    panel.set_objects([("A", {"object_id": "a"})])
    panel.set_operation("timeseries.lowpass")
    panel.fields["frequency"].setText("20")
    received = []
    panel.preview_requested.connect(received.append)
    panel.preview_recorded("filtered", {"key": "X"})
    assert received[0]["recorded_object_id"] == "filtered"
    assert received[0]["recorded_selector"] == {"key": "X"}
    panel.set_busy(False)
    panel.fields["frequency"].setText("30")
    panel.preview_button.click()
    assert "recorded_object_id" not in received[1]
    panel.close()


@pytest.mark.contract(id="GUI-SIG-PAN-008")
@pytest.mark.gui
def test_inspection_accepts_worker_normalized_paths_and_resolved_auto_format(qapp):
    """Confirm a normalized inspection only when the submitted form is unchanged."""
    from gwexpy_studio.ui.io_panel import DataIOPanel

    panel = DataIOPanel()
    panel.paths_edit.setPlainText("relative.csv")
    panel.inspect_button.click()
    normalized = {
        **panel.request(),
        "paths": ["/absolute/relative.csv"],
        "format": "csv",
    }
    panel.show_inspection({"request": normalized, "entry_count": 1})
    assert panel.confirm_button.isEnabled()
    assert '"format": "csv"' in panel.confirmation.toPlainText()
    panel.close()


@pytest.mark.contract(id="GUI-SIG-PAN-009")
@pytest.mark.gui
def test_quantity_fields_accept_explicit_units_and_reject_nonfinite_values(qapp):
    """Keep entered physical units in detached filter and FFT requests."""
    from gwexpy_studio.ui.parameter_panel import ParameterPanel

    panel = ParameterPanel()
    panel.set_objects([("A", {"object_id": "a"})])
    panel.set_operation("timeseries.lowpass")
    panel.fields["frequency"].setText("2 kHz")
    assert panel.request()["params"]["frequency"] == {"value": 2, "unit": "kHz"}
    panel.fields["frequency"].setText("nan Hz")
    with pytest.raises(ValueError, match="finite"):
        panel.request()
    panel.fields["frequency"].setText("20")
    assert panel.request()["params"]["frequency"] == {"value": 20, "unit": "Hz"}
    panel.set_operation("timeseries.psd")
    panel.fields["fftlength"].setText("500 ms")
    assert panel.request()["params"]["fftlength"] == {"value": 500, "unit": "ms"}
    panel.close()


@pytest.mark.contract(id="GUI-SIG-PAN-010")
@pytest.mark.gui
def test_io_panel_keeps_development_catalog_editable_and_native(qapp):
    """A catalog without artifact annotations retains the established source UX."""
    from gwexpy_studio.ui.io_panel import DataIOPanel

    panel = DataIOPanel(direction="read")
    panel.set_catalog(_native_catalog())
    assert _format_items(panel) == ["Auto", "hdf5"]
    assert panel.format_combo.isEditable()
    assert panel.capability_label.isHidden()
    assert panel.format_combo.toolTip() == (
        "Choose a registered candidate, Auto, or enter a format name."
    )
    panel.format_combo.setEditText("new-native-reader")
    panel.paths_edit.setPlainText("relative.data")
    assert panel.request()["format"] == "new-native-reader"
    panel.close()


@pytest.mark.contract(id="GUI-SIG-PAN-011")
@pytest.mark.gui
def test_frozen_io_catalog_labels_tiers_and_preserves_canonical_format(qapp):
    """Tier C is disabled while Tier B remains selectable with its caveat."""
    from PySide6.QtCore import Qt

    from gwexpy_studio.ui.io_panel import DataIOPanel

    panel = DataIOPanel(direction="read")
    panel.set_catalog(_frozen_catalog())
    assert _format_items(panel) == [
        "Auto",
        "hdf5",
        "csv",
        "mseed — Unavailable (backend not bundled)",
    ]
    unavailable = panel.format_combo.model().index(3, 0)
    assert not (
        panel.format_combo.model().flags(unavailable) & Qt.ItemFlag.ItemIsEnabled
    )
    assert "identified by the worker" in panel.capability_label.text()
    panel.format_combo.setCurrentIndex(2)
    panel.paths_edit.setPlainText("input.data")
    assert panel.request()["format"] == "csv"
    assert "Names and channels may be lost" in panel.capability_label.text()
    panel.close()


@pytest.mark.contract(id="GUI-SIG-PAN-012")
@pytest.mark.gui
def test_frozen_io_panel_rejects_auto_write_and_manual_tier_c(qapp):
    """Typed values cannot make an unavailable direction dispatch from the panel."""
    from PySide6.QtCore import Qt

    from gwexpy_studio.ui.io_panel import DataIOPanel

    panel = DataIOPanel(direction="write")
    panel.set_catalog(_frozen_catalog(direction="write"))
    assert _format_items(panel)[0] == "Auto — Unavailable (choose a format)"
    auto = panel.format_combo.model().index(0, 0)
    assert not (panel.format_combo.model().flags(auto) & Qt.ItemFlag.ItemIsEnabled)
    panel.paths_edit.setPlainText("output.data")
    dispatched: list[object] = []
    panel.inspect_requested.connect(dispatched.append)
    panel.format_combo.setEditText("mseed")
    panel.inspect_button.click()
    assert dispatched == []
    assert panel.error_label.text() == (
        "[io_capability_unavailable] Backend not included in this packaged build."
    )
    panel.format_combo.setEditText("Auto")
    panel.inspect_button.click()
    assert dispatched == []
    assert panel.error_label.text() == (
        "[io_capability_unavailable] Choose an explicit bundled format for export."
    )
    panel.close()


@pytest.mark.contract(id="GUI-SIG-PAN-013")
@pytest.mark.gui
def test_frozen_io_panel_never_displays_untrusted_catalog_paths(qapp):
    """Malformed presentation fields cannot leak a source path through the UI."""
    from gwexpy_studio.ui.io_panel import DataIOPanel

    sensitive_path = "/home/alice/private/experiment.gwf"
    catalog = _frozen_catalog()
    formats = catalog["formats"]
    assert isinstance(formats, list)
    formats[1]["capabilities"]["read"]["caveat"] = sensitive_path
    formats[2]["capabilities"]["read"]["reason"] = sensitive_path
    panel = DataIOPanel(direction="read")
    panel.set_catalog(catalog)
    panel.format_combo.setCurrentIndex(2)
    displayed = "\n".join(
        (
            *_format_items(panel),
            panel.capability_label.text(),
            panel.format_combo.toolTip(),
        )
    )
    assert sensitive_path not in displayed
    assert "metadata limitations" in panel.capability_label.text().lower()
    assert "Unavailable" in _format_items(panel)[3]
    panel.close()


@pytest.mark.contract(id="GUI-SIG-PAN-014")
@pytest.mark.gui
def test_frozen_io_panel_lists_manifest_entry_missing_from_worker_registry(qapp):
    """A vanished reviewed entry remains visible with its safe runtime reason."""
    from PySide6.QtCore import Qt

    from gwexpy_studio.ui.io_panel import DataIOPanel

    catalog = _frozen_catalog()
    formats = catalog["formats"]
    assert isinstance(formats, list)
    formats.append(
        {
            "format": "gone",
            "read": False,
            "write": False,
            "auto_identify": False,
            "capabilities": {
                "read": {
                    "available": False,
                    "tier": "A",
                    "caveat": None,
                    "reason": "registry_missing",
                    "status": "unavailable",
                },
                "write": {
                    "available": False,
                    "tier": "C",
                    "caveat": None,
                    "reason": "frozen_unverified",
                    "status": "unavailable",
                },
            },
        }
    )
    panel = DataIOPanel(direction="read")
    panel.set_catalog(catalog)

    assert "gone — Unavailable (registry entry missing)" in _format_items(panel)
    gone_index = _format_items(panel).index(
        "gone — Unavailable (registry entry missing)"
    )
    gone = panel.format_combo.model().index(gone_index, 0)
    assert not (panel.format_combo.model().flags(gone) & Qt.ItemFlag.ItemIsEnabled)
    panel.close()
