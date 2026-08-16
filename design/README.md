# Logo source files

SVG sources for the brand assets shipped in
`custom_components/ha_workouts/brand/`. Edit these, then re-render to PNG:

```bash
pip install cairosvg
python3 -c "
import cairosvg
cairosvg.svg2png(url='design/icon.svg', write_to='custom_components/ha_workouts/brand/icon.png', output_width=256, output_height=256)
cairosvg.svg2png(url='design/icon.svg', write_to='custom_components/ha_workouts/brand/icon@2x.png', output_width=512, output_height=512)
cairosvg.svg2png(url='design/icon.svg', write_to='custom_components/ha_workouts/brand/dark_icon.png', output_width=256, output_height=256)
cairosvg.svg2png(url='design/icon.svg', write_to='custom_components/ha_workouts/brand/dark_icon@2x.png', output_width=512, output_height=512)
cairosvg.svg2png(url='design/logo.svg', write_to='custom_components/ha_workouts/brand/logo.png', output_width=256, output_height=320)
cairosvg.svg2png(url='design/logo.svg', write_to='custom_components/ha_workouts/brand/logo@2x.png', output_width=512, output_height=640)
cairosvg.svg2png(url='design/dark_logo.svg', write_to='custom_components/ha_workouts/brand/dark_logo.png', output_width=256, output_height=320)
cairosvg.svg2png(url='design/dark_logo.svg', write_to='custom_components/ha_workouts/brand/dark_logo@2x.png', output_width=512, output_height=640)
"
```

`icon.svg` is the square badge mark alone. `logo.svg` / `dark_logo.svg` add
the "Workouts" wordmark below it, in dark/light text respectively for use
on light/dark backgrounds.
