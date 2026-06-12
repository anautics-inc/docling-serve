# tapp3
# Copyright 2018-2024 by Altair Engineering Inc.
#
# This example source code can be freely used and modified completely or
# in parts by customers of Altair Engineering Inc.
# It comes with no warranty of any kind and is provided "as-is".
#
# Title:
#      Tcl Test Application for Altair's Edb Creator API
#
# Description:
#      It creates an Edb and stores it into a file.
#
# Author:
#       Lothar Linhard, Ralf Wimmer


##
# Load the EdbCreator Tcl extension: (edbcreatorST.so)
#
load ./edbcreatorST[info sharedlibextension] EdbCreator

# Wire colors
set red_blue "#ff0000 #0000ff"
set green_orange "#00aa00 #ffa500"
set light_blue "#8888ff"

# Create an empty EDB
set edb [edb new]

# Create an ECU with one connector and five cavities
set comp [$edb new component -name "C1" -ecu]
set connA [$edb new connector $comp -name "A"]
set cav {}
foreach i {1 2 3 4} {
    lappend cav [$edb new cavity $connA -name $i]
}
set sh1 [$edb new cavity $connA -halfdot]

# Create an inliner with two connectors (one on each side)
set inl [$edb new component -name "I1" -inliner]
set connB [$edb new connector $inl -name "B" -female]
set connC [$edb new connector $inl -name "C" -male]
$edb partner $connB $connC

# ... and four cavities on each side (plus a shield on the left)
set cav_left {}
foreach i {1 2 3 4} {
    lappend cav_left [$edb new cavity $connB -name $i]
}

set cav_right {}
foreach i {1 2 3 4} {
    lappend cav_right [$edb new cavity $connC -name $i]
}
set sh2 [$edb new cavity $connB -halfdot]

foreach a $cav_left b $cav_right {
    $edb partner $a $b
}

# Connect the ECU with the left side of the inliner using four wires
set wires_left {}
set i 1
foreach a $cav b $cav_left {
    set wire [$edb new wire -name "W$i"]
    incr i
    $edb join $a $wire
    $edb join $b $wire
    lappend wires_left $wire
}

# Connect the shield cavities
set sh_wire [$edb new wire -name "SHL"]
$edb join $sh1 $sh_wire
$edb join $sh2 $sh_wire

# Create the multicores
# ... the outer one
set mc_outer [$edb new multicore 0 -name "MC_outer" -shielded]
$edb shield $sh_wire $mc_outer

# ... the two inner ones
set mc_inner1 [$edb new multicore $mc_outer -name "MC_inner1" -twshielded]
$edb group [lindex $wires_left 0] $mc_inner1
$edb group [lindex $wires_left 1] $mc_inner1

set mc_inner2 [$edb new multicore $mc_outer -name "MC_inner2" -twisted]
$edb group [lindex $wires_left 2] $mc_inner2
$edb group [lindex $wires_left 3] $mc_inner2

# Create a SPLICE component with three cavities
set sp [$edb new component -name S1 -splice]
set spC [$edb new connector $sp]
set sp_cav {}
foreach i {1 2 3} {
    lappend sp_cav [$edb new cavity $spC]
}

# Create an EYELET component with three cavities
set eye [$edb new component -name E1 -eyelet]
set eyeC [$edb new connector $eye]
set eye_cav {}
foreach i {1 2 3} {
    lappend eye_cav [$edb new cavity $eyeC]
}

# Create an ECU with an invisible connector and two cavities.
# Color the ECU in lightblue and assign it the symbol of the
# operating resource class "P".
set sensor [$edb new component -name "Sensor" -ecu]
$edb new attr $sensor " imagedsp" "P,40,40"
$edb new attr $sensor " color" $light_blue
set sensorC [$edb new connector $sensor -invisible]
set sensor_cavs {}
foreach i {1 2} {
    lappend sensor_cavs [$edb new cavity $sensorC -name $i]
}

# Now connect the INLINER, the SPLICE, the EYELET and the sensor
set wires {}
foreach i {5 6 7 8 9 10} {
    lappend wires [$edb new wire -name "W$i"]
}

# Assign color markers to the wires
foreach i {0 1 2} {
    $edb new attr [lindex $wires $i] " color" $red_blue
}
foreach i {3 4 5} {
    $edb new attr [lindex $wires $i] " color" $green_orange
}
$edb new attr [lindex $wires 3] "cross\u03c6" "2.5mm\u00b2 \u25a2"

# ... establish the connections
$edb join [lindex $cav_right 0] [lindex $wires 0]
$edb join [lindex $sp_cav 0] [lindex $wires 0]

$edb join [lindex $cav_right 1] [lindex $wires 1]
$edb join [lindex $sp_cav 1] [lindex $wires 1]

$edb join [lindex $sp_cav 2] [lindex $wires 2]
$edb join [lindex $sensor_cavs 0] [lindex $wires 2]

$edb join [lindex $cav_right 2] [lindex $wires 3]
$edb join [lindex $eye_cav 0] [lindex $wires 3]

$edb join [lindex $cav_right 3] [lindex $wires 4]
$edb join [lindex $eye_cav 1] [lindex $wires 4]

$edb join [lindex $eye_cav 2] [lindex $wires 5]
$edb join [lindex $sensor_cavs 1] [lindex $wires 5]

# Create a FUNCTION module
set fct [$edb new module -name "Processing" -function]
foreach obj [list $comp $connA [lindex $cav 0] [lindex $cav 1] \
    [lindex $wires_left 0] [lindex $wires_left 1] $inl \
    $connB $connC [lindex $cav_left 0] [lindex $cav_left 1] \
    [lindex $cav_right 0] [lindex $cav_right 1] $sp \
    [lindex $sp_cav 0] [lindex $sp_cav 1] [lindex $wires 0] \
    [lindex $wires 1] ] {
        $edb module add $fct $obj
    }

$edb save tapp3.edb
$edb delete
