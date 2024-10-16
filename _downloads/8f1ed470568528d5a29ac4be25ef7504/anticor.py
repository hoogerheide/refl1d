from refl1d.names import *

nickel = Material("Ni")

numpy.random.seed(5)

sample = silicon(0, 5) | nickel(100, 10) | nickel(100, 10) | air

sample[0].interface.range(0, 20)
sample[1].interface.range(0, 20)
sample[2].interface.range(0, 20)
sample[1].thickness.range(0, 400)
sample[2].thickness.range(0, 400)

T = numpy.linspace(0, 2, 200)
probe = NeutronProbe(T=T, dT=0.01, L=4.75, dL=0.0475)
M = Experiment(sample=sample, probe=probe)
M.simulate_data(noise=5)

problem = FitProblem(M)

