"""Paleta visual comercial e folhas de estilo Qt (QSS) do Universal Translator."""
from __future__ import annotations

PALETTE = {
    "bg_dark": "#101828",
    "surface_dark": "#1D2939",
    "secondary": "#344054",
    "surface_light": "#FFFFFF",
    "bg_light": "#F5F7FA",
    "primary": "#2563EB",
    "primary_hover": "#1D4ED8",
    "success": "#16A36A",
    "success_hover": "#128456",
    "warning": "#F79009",
    "error": "#D92D20",
    "text_primary": "#F2F4F7",
    "text_secondary": "#D0D5DD",
    "text_muted": "#98A2B3",
    "border": "#344054",
}

DARK_STYLESHEET = """
QWidget {
    background-color: #101828;
    color: #F2F4F7;
    font-family: 'Inter', 'Segoe UI', Arial, sans-serif;
    font-size: 13px;
}
QMainWindow, QDialog {
    background-color: #101828;
}
QTabWidget::pane {
    border: 1px solid #344054;
    border-radius: 8px;
    background-color: #101828;
    top: -1px;
}
QTabBar::tab {
    background-color: #1D2939;
    color: #D0D5DD;
    border: 1px solid #344054;
    border-bottom: none;
    padding: 9px 16px;
    margin-right: 4px;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
    font-weight: 600;
}
QTabBar::tab:selected {
    background-color: #2563EB;
    color: #FFFFFF;
    border-color: #2563EB;
}
QTabBar::tab:hover:!selected {
    background-color: #344054;
}
QFrame#CardFrame {
    background-color: #1D2939;
    border: 1px solid #344054;
    border-radius: 8px;
}
QLabel {
    color: #D0D5DD;
    background: transparent;
}
QLabel#HeaderTitle {
    font-size: 20px;
    font-weight: 700;
    color: #FFFFFF;
}
QLabel#SubTitle {
    font-size: 12px;
    color: #98A2B3;
}
QLabel#BadgeLabel {
    background-color: #2563EB;
    color: #FFFFFF;
    padding: 4px 10px;
    border-radius: 11px;
    font-size: 11px;
    font-weight: 600;
}
QLineEdit, QComboBox, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox {
    background-color: #1D2939;
    border: 1px solid #344054;
    padding: 8px 10px;
    border-radius: 6px;
    color: #FFFFFF;
    selection-background-color: #2563EB;
}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {
    border: 1px solid #2563EB;
}
QPushButton {
    background-color: #2563EB;
    color: #FFFFFF;
    border: none;
    padding: 9px 14px;
    border-radius: 6px;
    font-weight: 600;
}
QPushButton:hover {
    background-color: #1D4ED8;
}
QPushButton:disabled {
    background-color: #344054;
    color: #98A2B3;
}
QPushButton#SuccessButton {
    background-color: #16A36A;
}
QPushButton#SuccessButton:hover {
    background-color: #128456;
}
QPushButton#SecondaryButton {
    background-color: #344054;
    color: #F2F4F7;
}
QPushButton#SecondaryButton:hover {
    background-color: #475467;
}
QPushButton#DangerButton {
    background-color: #D92D20;
    color: #FFFFFF;
}
QPushButton#DangerButton:hover {
    background-color: #B42318;
}
QTableWidget {
    background-color: #1D2939;
    alternate-background-color: #162030;
    border: 1px solid #344054;
    border-radius: 6px;
    gridline-color: #344054;
    color: #F2F4F7;
}
QHeaderView::section {
    background-color: #344054;
    color: #FFFFFF;
    padding: 7px;
    border: none;
    font-weight: 600;
}
QCheckBox {
    color: #D0D5DD;
    spacing: 8px;
}
"""
