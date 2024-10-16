from refl1d.names import *

SiOx = Mixture.byvolume("Si@2.329", "SiO2@26.5", 50, name="SiOx")

nickel = Material("Ni")
sample = silicon(0, 5) | SiOx(10, 2) | nickel(125, 10) | air

sample["Ni"].thickness.pm(50)
sample["Si"].interface.range(0, 12)
sample["Ni"].interface.range(0, 20)

sample["SiOx"].interface.range(0, 12)
sample["SiOx"].thickness.range(0, 20)
sample["SiOx"].material.fraction[0].range(0, 100)

instrument = SNS.Liquids()
files = ["nifilm-tof-%d.dat" % d for d in (1, 2, 3, 4)]
probe = ProbeSet(instrument.load(f) for f in files)

M = Experiment(probe=probe, sample=sample)

problem = FitProblem(M)
