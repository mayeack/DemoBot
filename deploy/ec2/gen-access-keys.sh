#!/bin/bash
# Generate per-replica ACCESS_KEY values in DemoBot's established four-word
# format (goose-duck-shovel-blob) and record them in deploy/ec2/access-keys.env.
#
#   ./deploy/ec2/gen-access-keys.sh 6          # ensure keys 1..6 exist
#   ./deploy/ec2/gen-access-keys.sh --show     # print what is on file
#   ./deploy/ec2/gen-access-keys.sh --rotate 3 # replace ONLY replica 3's key
#
# WHY THIS EXISTS: DemoBot has no key generator. backend/config.py declares
# `access_key: str = ""` and reads it from .env at startup — nothing generates
# it, nothing stores it, and there is no wordlist in the repo. The four-word
# shape is a convention applied by hand. This script makes that convention
# reproducible per replica.
#
# IDEMPOTENT BY DESIGN: an existing ACCESS_KEY_<N> is never silently replaced.
# Re-running only fills gaps, so `deploy` can call it freely without changing
# credentials out from under a workshop that is already using them. Use
# --rotate to deliberately replace one.
#
# The word list is a curated set of short concrete nouns. /usr/share/dict/words
# is deliberately NOT used: it is the 236k-entry unabridged dictionary and
# produces keys like "zygodactylous-phlebotomize-…" that nobody can read aloud
# to a workshop room.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
KEYFILE="$REPO/deploy/ec2/access-keys.env"

log()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

COUNT=""; ROTATE=""; SHOW=false
case "${1:-}" in
  --show)   SHOW=true ;;
  --rotate) ROTATE="${2:-}"; [ -n "$ROTATE" ] || die "--rotate needs a replica number" ;;
  -h|--help|"") sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  ''|*[!0-9]*) die "expected a replica count, --show, or --rotate N (try --help)" ;;
  *) COUNT="$1" ;;
esac

# --- the generator ---------------------------------------------------------
# secrets.choice, not random.choice: these gate a publicly reachable URL, and
# random is a Mersenne Twister seeded from time — predictable given a sample.
gen_key() {
  python3 - <<'PY'
import secrets
WORDS = """
acorn alarm album amber anchor ankle apple apron arrow attic
bacon badge bagel baker balcony ballot bamboo banjo barge barrel
basil basket batch beacon beetle bellow bench berry bishop bison
blade blanket blob blossom board bobbin bolt bonnet booth border
bottle boulder bracket braid branch brass bread brick bridge broom
brush bubble bucket buckle buffalo bugle bundle bunker burrow button
cabin cable cactus camel candle canoe canvas canyon carpet carrot
castle cattle cauldron cavern cedar cellar chalk chapel charm cheese
cherry chimney chisel cider cinder circus clamp clatter cliff cloak
clover cluster cobweb cocoa collar column comet compass cone copper
coral cottage cotton crane crater crayon creek crest cricket crown
crumb crystal cushion cymbal dagger dairy daisy dial diamond ditch
dolphin domino donkey drawer drift drum dune dusk eagle easel
ember emblem engine envelope falcon fathom feather fennel fern ferry
fiddle filter finch flagon flannel flask fleece flint flute forest
fossil fountain fox foxglove frost furnace gable gallon garden garlic
gavel gazelle geyser ginger glacier glider globe glove gnome goblet
goose gopher gourd granite gravel grotto grove gully gutter hammer
hamlet harbor harvest hatch hazel heather hedge helmet heron hickory
hinge hollow honey hornet horizon hurdle igloo indigo iris island
ivory jacket jasmine jelly jetty jigsaw jungle juniper kayak kernel
kettle keystone kitten knapsack knuckle lantern lattice lavender ledge lemon
lentil lever lichen lilac linen lizard lobby locket lodge lumber
magnet mallet mango mantle maple marble marsh meadow medal melon
mermaid meteor mitten moat mongoose moss mountain muffin mulberry mushroom
nectar needle nickel noodle nutmeg oasis oatmeal obelisk olive onion
orchard organ osprey otter outpost owl oyster paddle palace pancake
panther papaya parcel parlor parsnip pasture peach pebble pelican pencil
pepper petal pewter piano pigeon pillar pilot pine pistol pitcher
plank plateau plum pocket pollen pond poplar poppy porch portal
pottery prairie pretzel prism pudding puffin pumpkin puzzle quarry quartz
quiver rabbit radish rafter ragweed rainbow rampart ranch raven ravine
razor reef relic ribbon ridge rifle river robin rocket roost
rosemary rudder ruffle rug rune saddle saffron sage sailor salmon
sandal sapling satchel saucer sawmill scale scarf scepter scissors scooter
scroll seagull seashell sequoia shackle shadow shale shallot shamrock shanty
shovel shutter sickle silo silver siren skillet skylark slate sledge
sliver smoke snail sparrow spindle spiral spool sprout spruce squash
stable stallion starling steeple stencil stirrup stone stork stove stream
stucco stump sugar sulfur summit sundial swallow swamp sycamore syrup
tabby tackle talon tambourine tangerine tapestry tavern teapot temple tendril
thicket thimble thistle thorn thunder ticket timber tinder toffee token
tomato topaz torch tortoise tower trellis triangle trickle trolley trout
trumpet tulip tundra tunnel turban turnip turtle tusk umbrella valley
vane vanilla velvet vessel village vine violet vulture waffle wagon
walnut walrus warbler wattle weasel whistle willow window wombat wrench
yarrow yeast yonder zebra zinnia zipper
"""
words = WORDS.split()
print("-".join(secrets.choice(words) for _ in range(4)))
PY
}

# --- read existing ---------------------------------------------------------
existing_key() {   # $1 = replica number
  [ -f "$KEYFILE" ] || return 1
  grep -E "^ACCESS_KEY_$1=" "$KEYFILE" 2>/dev/null | head -1 | cut -d= -f2-
}

ensure_keyfile() {
  if [ ! -f "$KEYFILE" ]; then
    umask 077
    {
      echo "# DemoBot per-replica ACCESS_KEY values."
      echo "# Generated by deploy/ec2/gen-access-keys.sh — DO NOT COMMIT."
      echo "# push-replica.sh reads the line matching its --replica N."
    } > "$KEYFILE"
    chmod 600 "$KEYFILE"
  fi
}

if [ "$SHOW" = true ]; then
  [ -f "$KEYFILE" ] || die "no $KEYFILE yet — generate some first"
  log "access keys on file"
  grep -E '^ACCESS_KEY_[0-9]+=' "$KEYFILE" | sort -t_ -k3 -n | sed 's/^/  /'
  exit 0
fi

ensure_keyfile

if [ -n "$ROTATE" ]; then
  old=$(existing_key "$ROTATE" || true)
  [ -n "$old" ] || die "no existing key for replica $ROTATE — nothing to rotate"
  new=$(gen_key)
  # Rewrite in place, preserving every other line.
  tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
  awk -v n="$ROTATE" -v k="$new" \
      '{ if ($0 ~ "^ACCESS_KEY_" n "=") print "ACCESS_KEY_" n "=" k; else print }' \
      "$KEYFILE" > "$tmp"
  cat "$tmp" > "$KEYFILE"; chmod 600 "$KEYFILE"
  log "rotated replica $ROTATE"
  echo "  old: $old"
  echo "  new: $new"
  warn "the box keeps serving the OLD key until it is re-pushed and the app restarts.
      Re-deploy replica $ROTATE, then verify old->401 / new->200."
  exit 0
fi

# --- fill gaps up to COUNT -------------------------------------------------
log "ensuring access keys for replicas 1..$COUNT"
added=0
for n in $(seq 1 "$COUNT"); do
  if k=$(existing_key "$n") && [ -n "$k" ]; then
    printf '  %-16s %s  (existing)\n' "ACCESS_KEY_$n" "$k"
  else
    k=$(gen_key)
    printf 'ACCESS_KEY_%s=%s\n' "$n" "$k" >> "$KEYFILE"
    printf '  %-16s %s  \033[32m(new)\033[0m\n' "ACCESS_KEY_$n" "$k"
    added=$((added + 1))
  fi
done
chmod 600 "$KEYFILE"

echo
echo "  $added new, $((COUNT - added)) already on file -> $KEYFILE"
echo
echo "  These are the Basic-auth passwords for each box:"
echo "     curl -u x:\$ACCESS_KEY_1 https://medadvice1.yeackbot.com/health"
echo "  Record them wherever you keep workshop credentials. The file is mode 600"
echo "  and gitignored; it is the only copy."
