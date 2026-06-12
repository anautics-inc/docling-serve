################################################################################
# Tapp3
# Copyright 2020-2024 by Altair Engineering Inc.
#
# This example source code can be freely used and modified completely or
# in parts by customers of Altair Engineering Inc.
# It comes with no warranty of any kind and is provided "as-is".
#
# Title:
#      Python Test Application for Altair's Edb Creator API
#
# Description:
#      It creates and loads an Edb and stores it into a file.
#
# Arguments:
#      ?-silent? ?output_filename.edb?
#
# Author:
#       Lothar Linhard, Ralf Wimmer
"""Test Application for Creator API for Python."""

import sys
from PedbCreator import Edb, \
    EdbConnectorType, EdbComponentType, EdbCavityType, \
    EdbMulticoreType, EdbModuleType


# Wire colors
RED_BLUE = "#ff0000 #0000ff"
GREEN_ORANGE = "#00aa00 #ffa500"
LIGHT_BLUE = "#8888ff"

# Create an empty EDB
edb = Edb()

# Create an ECU with one connector and five cavities
comp = edb.NewComponent("C1", EdbComponentType.ECU)
connA = edb.NewConnector(comp, "A")
cav = [edb.NewCavity(connA, str(i)) for i in range(1, 5)]
sh1 = edb.NewCavity(connA, None, EdbCavityType.HALFDOT)

# Create an inliner with two connectors (one on each side)
inl = edb.NewComponent("I1", EdbComponentType.INLINER)
connB = edb.NewConnector(inl, "B", EdbConnectorType.FEMALE)
connC = edb.NewConnector(inl, "C", EdbConnectorType.MALE)
edb.PartnerConnector(connB, connC)

# ... and four cavities on each side (plus a shield on the left)
cav_left = [edb.NewCavity(connB, str(i)) for i in range(1, 5)]
cav_right = [edb.NewCavity(connC, str(i)) for i in range(1, 5)]
sh2 = edb.NewCavity(connB, None, EdbCavityType.HALFDOT)

for (left, right) in zip(cav_left, cav_right):
    edb.PartnerCavity(left, right)

# Connect the ECU with the left side of the inliner using four wires
wires_left = []
for (left, right, i) in zip(cav, cav_left, range(len(cav))):
    wire = edb.NewWire("W" + str(i+1))
    edb.Join(left, wire)
    edb.Join(right, wire)
    wires_left.append(wire)

# Connect the shield cavities
sh_wire = edb.NewWire("SHL")
edb.Join(sh1, sh_wire)
edb.Join(sh2, sh_wire)

# Create the multicores
# ... the outer one
mc_outer = edb.NewMulticore(None, "MC_outer", EdbMulticoreType.SHIELDED)
edb.ShieldWire(sh_wire, mc_outer)

# ... the two inner ones
mc_inner1 = edb.NewMulticore(mc_outer, "MC_inner1", EdbMulticoreType.TWSHIELDED)
edb.GroupWire(wires_left[0], mc_inner1)
edb.GroupWire(wires_left[1], mc_inner1)

mc_inner2 = edb.NewMulticore(mc_outer, "MC_inner2", EdbMulticoreType.TWISTED)
edb.GroupWire(wires_left[2], mc_inner2)
edb.GroupWire(wires_left[3], mc_inner2)


# Create a SPLICE component with three cavities
sp = edb.NewComponent("S1", EdbComponentType.SPLICE)
spC = edb.NewConnector(sp, None)
sp_cav = [edb.NewCavity(spC, None) for i in range(3)]

# Create an EYELET component with three cavities
eye = edb.NewComponent("E1", EdbComponentType.EYELET)
eyeC = edb.NewConnector(eye, None)
eye_cav = [edb.NewCavity(eyeC, None) for i in range(3)]

# Create an ECU with an invisible connector and two cavities.
# Color the ECU in lightblue and assign it the symbol of the
# operating resource class "P".
sensor = edb.NewComponent("Sensor", EdbComponentType.ECU)
edb.NewAttr(sensor, " imagedsp", "P,40,40")
edb.NewAttr(sensor, " color", LIGHT_BLUE)
sensorC = edb.NewConnector(sensor, None, EdbConnectorType.INVISIBLE)
sensor_cavs = [edb.NewCavity(sensorC, str(i+1)) for i in range(2)]

# Now connect the INLINER, the SPLICE, the EYELET and the sensor
wires = [edb.NewWire("W" + str(i+5)) for i in range(6)]
# ... assign color markers to the wires
for i in range(3):
    edb.NewAttr(wires[i], " color", RED_BLUE)

for i in range(3, 6):
    edb.NewAttr(wires[i], " color", GREEN_ORANGE)
edb.NewAttr(wires[3], "cross\u03c6", "2.5mm\u00b2 \u25a2")

# ... establish the connections
edb.Join(cav_right[0], wires[0])
edb.Join(sp_cav[0], wires[0])

edb.Join(cav_right[1], wires[1])
edb.Join(sp_cav[1], wires[1])

edb.Join(sp_cav[2], wires[2])
edb.Join(sensor_cavs[0], wires[2])

edb.Join(cav_right[2], wires[3])
edb.Join(eye_cav[0], wires[3])

edb.Join(cav_right[3], wires[4])
edb.Join(eye_cav[1], wires[4])

edb.Join(eye_cav[2], wires[5])
edb.Join(sensor_cavs[1], wires[5])

# Create a FUNCTION module
fct = edb.NewModule("Processing", EdbModuleType.FUNCTION)
for obj in [comp, connA, cav[0], cav[1], wires_left[0], wires_left[1], \
        inl, connB, connC, cav_left[0], cav_left[1], cav_right[0], \
        cav_right[1], sp, sp_cav[0], sp_cav[1], wires[0], \
        wires[1]]:
    edb.AddObject2Module(fct, obj)


# Save the EDB to the file specified on the command line
silent = False
for arg in sys.argv[1:]:
    if arg == "-silent":
        silent = True
        continue
    edb.SaveFile(arg)
    if not silent:
        print("EDB saved to: " + arg)

if not silent:
    print("EDB Version : " + edb.Version())
