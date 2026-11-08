"""Rebuild the fictional two-page PDF used by the demo seed.

Requires reportlab and fonttools. Run from the repository root:
    python data/demo/generate_it_badge_pdf.py
"""
from pathlib import Path
import os

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


OUTPUT = Path(__file__).resolve().parent / "docs" / "it-badge.pdf"


def main() -> None:
    candidates = [Path(os.environ["KNV_DEMO_FONT"])] if os.environ.get("KNV_DEMO_FONT") else [
        Path("C:/Windows/Fonts/NotoSansSC-VF.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    ]
    font_path = next((path for path in candidates if path.is_file()), None)
    if font_path is None:
        raise SystemExit("Set KNV_DEMO_FONT to a Chinese TrueType font file")
    # The installed Noto variable font defaults to weight 100. Freeze a readable
    # medium weight before embedding; keep the generated static font out of git.
    if font_path.name == "NotoSansSC-VF.ttf":
        from fontTools.ttLib import TTFont as VariableFont
        from fontTools.varLib.instancer import instantiateVariableFont

        temp_font = OUTPUT.with_suffix(".font.tmp.ttf")
        variable = VariableFont(str(font_path))
        try:
            instantiateVariableFont(variable, {"wght": 500}, inplace=True).save(str(temp_font))
        finally:
            variable.close()
        try:
            pdfmetrics.registerFont(TTFont("DemoCJK", str(temp_font)))
        finally:
            temp_font.unlink(missing_ok=True)
    else:
        pdfmetrics.registerFont(TTFont("DemoCJK", str(font_path)))
    pdf = canvas.Canvas(str(OUTPUT), pagesize=A4, invariant=1)
    pdf.setTitle("访客证件与门禁操作说明（虚构演示）")
    pdf.setAuthor("示例精密设备公司 · IT")
    width, height = A4

    def page(number: int, heading: str, rows: list[str]) -> None:
        pdf.setFillColorRGB(0.09, 0.16, 0.27)
        pdf.rect(0, height - 22, width, 22, fill=1, stroke=0)
        pdf.setFont("DemoCJK", 20)
        pdf.drawString(54, height - 75, "访客证件与门禁操作说明")
        pdf.setFont("DemoCJK", 12)
        pdf.drawString(54, height - 105, heading)
        pdf.setStrokeColorRGB(0.75, 0.8, 0.84)
        pdf.line(54, height - 119, width - 54, height - 119)
        pdf.setFont("DemoCJK", 11)
        for index, row in enumerate(rows):
            pdf.drawString(54, height - 157 - index * 34, row)
        pdf.setFont("DemoCJK", 9)
        pdf.drawString(54, 45, "仅供 Knoventra 演示；公司、流程和联系方式均为虚构。")
        pdf.drawRightString(width - 54, 45, str(number))
        pdf.showPage()

    page(1, "1. 访客证件领取", [
        "访客须由接待员工在 IT 门户登记姓名、到访日期和接待部门。",
        "到访当天，接待员工在一层前台核对登记信息并领取临时访客证。",
        "访客证仅在登记日期有效；进入办公区时须由接待员工陪同。",
    ])
    page(2, "2. 遗失与归还", [
        "访客证遗失时，接待员工应立即联系 IT 服务台冻结该证权限。",
        "离开前，访客须将临时访客证交还一层前台，由接待员工确认归还。",
        "IT 服务台只处理证件权限；楼宇安全事件应联系现场安保。",
    ])
    pdf.save()
    print(OUTPUT)


if __name__ == "__main__":
    main()
