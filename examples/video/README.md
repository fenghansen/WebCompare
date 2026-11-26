# VideoMatte240K comparison sample

The subject and alpha matte come from clip `0001` in the 720p VideoMatte240K training split. We took matching source frames 0, 2, …, 28 from the 30 fps pair. The subject is composited over an illustrated room with a window, wall and floor. The composite and matte are resized to 768 × 432 before generating the two masked views.

| File | Pixels before H.264 encoding |
| --- | --- |
| [01-rgb.mp4](01-rgb.mp4) | The composited color image `RGB` |
| [02-pha.mp4](02-pha.mp4) | The 8-bit alpha matte `pha` as grayscale |
| [03-foreground.mp4](03-foreground.mp4) | `round(RGB × pha / 255)` |
| [04-background-region.mp4](04-background-region.mp4) | `round(RGB × (255 − pha) / 255)` |

The four MP4s are silent H.264 YUV 4:2:0 clips with 15 frames at 15 fps. Before encoding, the two masked views sum pixel by pixel to the composite.

Source: Shanchuan Lin, Andrey Ryabtsev, Soumyadip Sengupta, Brian L. Curless, Steven M. Seitz, and Ira Kemelmacher-Shlizerman, *Real-Time High-Resolution Background Matting*, CVPR 2021. [Paper](https://openaccess.thecvf.com/content/CVPR2021/html/Lin_Real-Time_High-Resolution_Background_Matting_CVPR_2021_paper.html) · [Dataset](https://grail.cs.washington.edu/projects/background-matting-v2/#/datasets) · [Maintainer’s license confirmation](https://github.com/PeterL1n/BackgroundMattingV2/issues/208#issuecomment-2038469807) · [Upstream MIT notice](LICENSE)
