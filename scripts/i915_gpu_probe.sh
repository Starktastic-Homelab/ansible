#!/bin/sh
set -eu

ffmpeg="${1:-/usr/lib/jellyfin-ffmpeg/ffmpeg}"
render=/dev/dri/renderD128
tmp=$(mktemp -d /tmp/i915-probe.XXXXXX)
trap 'rm -f "$tmp/h264.progress" "$tmp/hevc.progress" "$tmp/hdr.progress" "$tmp/hdr.mkv"; rmdir "$tmp"' EXIT

frames() {
  count=$(awk -F= '$1 == "frame" { frames = $2 } END { print frames + 0 }' "$1")
  if [ "$count" -lt 30 ]; then
    printf 'GPU probe produced only %s frames: %s\n' "$count" "$1" >&2
    return 1
  fi
  printf '%s' "$count"
}

timeout 30 "$ffmpeg" -nostdin -hide_banner -loglevel error \
  -init_hw_device "vaapi=va:$render" -filter_hw_device va \
  -f lavfi -i testsrc2=size=128x128:rate=30 \
  -vf format=nv12,hwupload -c:v h264_vaapi -low_power 1 \
  -frames:v 30 -progress "$tmp/h264.progress" -f null -
h264_frames=$(frames "$tmp/h264.progress")

timeout 30 "$ffmpeg" -nostdin -hide_banner -loglevel error \
  -init_hw_device "vaapi=va:$render" -filter_hw_device va \
  -f lavfi -i testsrc2=size=128x128:rate=30 \
  -vf format=nv12,hwupload -c:v hevc_vaapi -low_power 1 \
  -frames:v 30 -progress "$tmp/hevc.progress" -f null -
hevc_frames=$(frames "$tmp/hevc.progress")

# A synthetic Main10/PQ clip exercises hardware decode and VPP without media access.
timeout 30 "$ffmpeg" -nostdin -hide_banner -loglevel error \
  -f lavfi -i testsrc2=size=256x144:rate=30 -frames:v 30 \
  -vf format=yuv420p10le -c:v libx265 -preset ultrafast -threads 1 \
  -color_primaries bt2020 -color_trc smpte2084 -colorspace bt2020nc \
  -x265-params 'pools=1:frame-threads=1:log-level=error:master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1):max-cll=1000,400' \
  "$tmp/hdr.mkv"

timeout 30 "$ffmpeg" -nostdin -hide_banner -loglevel error \
  -init_hw_device "vaapi=va:$render" -filter_hw_device va \
  -hwaccel vaapi -hwaccel_device va -hwaccel_output_format vaapi \
  -i "$tmp/hdr.mkv" \
  -vf 'tonemap_vaapi=format=nv12:p=bt709:t=bt709:m=bt709,scale_vaapi=w=128:h=128' \
  -c:v h264_vaapi -low_power 1 -frames:v 30 \
  -progress "$tmp/hdr.progress" -f null -
hdr_frames=$(frames "$tmp/hdr.progress")

printf 'I915_GPU_RESULT={"node":"%s","kernel":"%s","module":"%s","boot_id":"%s","uid":%s,"frames":{"h264_low_power":%s,"hevc_low_power":%s,"hdr_decode_tonemap_encode":%s}}\n' \
  "${I915_NODE_NAME:?Downward API node identity is required}" \
  "$(uname -r)" "$(cat /sys/module/i915/version)" \
  "$(cat /proc/sys/kernel/random/boot_id)" "$(id -u)" \
  "$h264_frames" "$hevc_frames" "$hdr_frames"
