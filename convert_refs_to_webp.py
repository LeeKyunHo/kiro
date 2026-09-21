"""참조 이미지를 webp로 변환"""
from PIL import Image
from pathlib import Path

ref_dir = Path("c:/Users/rbsgh/kiro/projects/dark_generals/references")
png_files = list(ref_dir.glob("*.png"))

for png_file in png_files:
    webp_file = png_file.with_suffix(".webp")
    print(f"Converting {png_file.name} -> {webp_file.name}")
    
    img = Image.open(png_file)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    
    img.save(webp_file, "WEBP", quality=90, method=6)
    png_file.unlink()  # 원본 png 삭제
    print(f"  ✓ Done")

print(f"\n변환 완료: {len(png_files)}개 파일")
