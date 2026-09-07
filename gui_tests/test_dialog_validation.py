"""Validation behavior for the alpha operation parameter dialogs."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox

from gwexpy_studio.ui import dialogs


def _show(dialog: QDialog, qapp: QApplication) -> None:
    dialog.show()
    qapp.processEvents()


def _validation_text(dialog: QDialog) -> str:
    labels = dialog.findChildren(QLabel, "validationError")
    assert len(labels) == 1
    assert labels[0].isVisible()
    return labels[0].text()


@pytest.mark.gui
@pytest.mark.contract(id="GUI-VAL-001")
def test_invalid_numeric_input_shows_inline_error_without_modal_warning(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Numeric rejection is visible in the dialog and does not open a warning."""
    warnings: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args: warnings.append(args),
    )
    dialog = dialogs.AsdDialog()
    _show(dialog, qapp)
    dialog.fftlength_edit.setText("not-a-number")

    dialog._accept_if_valid()

    assert dialog.result() == QDialog.DialogCode.Rejected
    assert "FFT length" in _validation_text(dialog)
    assert warnings == []
    assert dialog.fftlength_edit.text() == "not-a-number"
    dialog.close()


@pytest.mark.gui
@pytest.mark.contract(id="GUI-VAL-002")
def test_crop_requires_start_before_end(qapp: QApplication) -> None:
    """Crop rejects inverted and equal supplied bounds."""
    for start, end in (("2", "1"), ("2", "2")):
        dialog = dialogs.CropDialog()
        _show(dialog, qapp)
        dialog.start_edit.setText(start)
        dialog.end_edit.setText(end)

        dialog._accept_if_valid()

        assert dialog.result() == QDialog.DialogCode.Rejected
        assert "before" in _validation_text(dialog).lower()
        dialog.close()


@pytest.mark.gui
@pytest.mark.contract(id="GUI-VAL-003")
def test_asd_requires_finite_positive_fftlength(qapp: QApplication) -> None:
    """ASD rejects missing, non-finite, non-numeric, and non-positive FFT lengths."""
    for value in ("", "0", "-1", "nan", "inf", "not-a-number"):
        dialog = dialogs.AsdDialog()
        _show(dialog, qapp)
        dialog.fftlength_edit.setText(value)

        dialog._accept_if_valid()

        assert dialog.result() == QDialog.DialogCode.Rejected
        assert "FFT length" in _validation_text(dialog)
        dialog.close()


@pytest.mark.gui
@pytest.mark.contract(id="GUI-VAL-004")
def test_spectrogram_requires_finite_positive_stride(qapp: QApplication) -> None:
    """Spectrogram rejects missing, non-finite, non-numeric, and non-positive stride."""
    for value in ("", "0", "-1", "nan", "inf", "not-a-number"):
        dialog = dialogs.SpectrogramDialog()
        _show(dialog, qapp)
        dialog.stride_edit.setText(value)

        dialog._accept_if_valid()

        assert dialog.result() == QDialog.DialogCode.Rejected
        assert "Stride" in _validation_text(dialog)
        dialog.close()


@pytest.mark.gui
@pytest.mark.contract(id="GUI-VAL-005")
def test_spectral_dialogs_validate_overlap_against_fftlength(
    qapp: QApplication,
) -> None:
    """Spectral dialogs require non-negative overlap shorter than FFT length."""
    cases: tuple[tuple[Callable[[], dialogs._SpectrumDialog], str, str], ...] = (
        (dialogs.AsdDialog, "1", "-0.1"),
        (dialogs.AsdDialog, "1", "1"),
        (dialogs.AsdDialog, "1", "2"),
        (dialogs.SpectrogramDialog, "1", "-0.1"),
        (dialogs.SpectrogramDialog, "1", "1"),
        (dialogs.SpectrogramDialog, "1", "2"),
    )
    for dialog_factory, fftlength, overlap in cases:
        dialog = dialog_factory()
        _show(dialog, qapp)
        if isinstance(dialog, dialogs.SpectrogramDialog):
            dialog.stride_edit.setText("1")
        dialog.fftlength_edit.setText(fftlength)
        dialog.overlap_edit.setText(overlap)

        dialog._accept_if_valid()

        assert dialog.result() == QDialog.DialogCode.Rejected
        assert "overlap" in _validation_text(dialog).lower()
        dialog.close()


@pytest.mark.gui
@pytest.mark.contract(id="GUI-VAL-006")
def test_corrected_input_is_accepted_with_raw_mapping(
    qapp: QApplication,
) -> None:
    """After an inline rejection, corrected values remain editable and are accepted."""
    dialog = dialogs.SpectrogramDialog()
    _show(dialog, qapp)
    dialog.stride_edit.setText("0")
    dialog.fftlength_edit.setText("2")
    dialog.overlap_edit.setText("0.5")

    dialog._accept_if_valid()
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert dialog.stride_edit.text() == "0"

    dialog.stride_edit.setText("1")
    dialog._accept_if_valid()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dict(dialog.params) == {
        "stride": 1.0,
        "fftlength": 2.0,
        "overlap": 0.5,
    }
    dialog.close()


@pytest.mark.gui
@pytest.mark.contract(id="GUI-VAL-007")
def test_optional_spectral_values_are_omitted_when_blank(qapp: QApplication) -> None:
    """Blank optional fields preserve the raw mapping omission semantics."""
    dialog = dialogs.SpectrogramDialog()
    _show(dialog, qapp)
    dialog.stride_edit.setText("1")

    dialog._accept_if_valid()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dict(dialog.params) == {"stride": 1.0}
    dialog.close()


@pytest.mark.gui
@pytest.mark.contract(id="GUI-VAL-008")
def test_cancel_does_not_create_params(qapp: QApplication) -> None:
    """Cancel leaves the dialog rejected and without an accepted mapping."""
    dialog = dialogs.CropDialog()
    _show(dialog, qapp)
    dialog.start_edit.setText("1")
    dialog.reject()

    assert dialog.result() == QDialog.DialogCode.Rejected
    with pytest.raises(RuntimeError, match="not been accepted"):
        _ = dialog.params
    dialog.close()
