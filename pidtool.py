#!/usr/bin/env python

from __future__ import print_function
import argparse
import numpy as np
from numpy.random import choice
import logging
import json


logging.basicConfig(level=logging.INFO)

class Resampler:

    def __init__(self, *args):
        # Choose histogram size according to bin edges
        # Take under/overflow into account for dependent variables only
        edges = []
        for arg in args[:-1]:
            edges.append(np.append(np.append([-np.inf], arg), [np.inf]))
        edges.append(args[-1])
        self.edges = edges

        self.histogram = np.zeros(map(lambda x: len(x) - 1, self.edges))

    def learn(self, features, weights=None):
        assert(len(features) == len(self.edges))

        features = np.array(features)

        h , _ = np.histogramdd(features.T, bins=self.edges, weights=weights)
        self.histogram += h

    def sample(self, features):

        assert(len(features) == len(self.edges) - 1)
        args = np.array(features)
        idx = [np.searchsorted(edges, vals) - 1 for edges, vals in zip(self.edges, args)]
        tmp = self.histogram[idx]
        # Fix negative bins (resulting from possible negative weights) to zero
        tmp[tmp < 0] = 0
        norm = np.sum(tmp, axis=1)
        probs = tmp / norm[:,np.newaxis]
        sampled_bin = []
        for i in range(tmp.shape[0]):
            sampled_bin.append(choice(tmp.shape[1], p=probs[i,:]))
        sampled_bin = np.array(sampled_bin)
        sampled_val = np.random.uniform(self.edges[-1][sampled_bin],
                                        self.edges[-1][sampled_bin + 1],
                                        size=len(sampled_bin))
        # If the histogram is empty, we can't sample
        sampled_val[norm == 0] = np.nan

        return sampled_val

def rooBinning_to_list(rooBinning):
    return [rooBinning.binLow(i) for i in range(rooBinning.numBins())]+[rooBinning.binHigh(rooBinning.numBins()-1)]

def grab_data(options):
    from root_pandas import read_root
    from pandas import DataFrame
    import ROOT
    from ROOT import TFile
    import subprocess
    import re

    def wrap_iter(it):
        elem = it.Next()
        while elem:
            yield elem
            elem = it.Next()

    logging.info("Saving nTuples to " + options.output)

    with open('raw_data.json') as f:
        locations = json.load(f)
    if options.particles is not None:
        locations =  [sample for sample in locations if sample["particle"] in options.particles]

    for sample in locations:
        output = options.output +'/{particle}_Stripping{stripping}_Magnet{magnet}.root'.format(**sample)
        # Create empty ROOT file
        ff = TFile(output, 'recreate')
        ff.Close()
        for input in sample['paths']:
            logging.info('Opening file {}'.format(input))
            f = TFile.Open(input)
            ROOT.SetOwnership(f, False)
            ws = f.Get(f.GetListOfKeys().First().GetName())
            ROOT.SetOwnership(ws, False)
            datalist = ws.allData()
            data = datalist.front()
            ROOT.SetOwnership(data, False)
            ROOT.RooAbsData.setDefaultStorageType(ROOT.RooAbsData.Tree)
            ROOT.SetOwnership(ff, False)
            dset = ROOT.RooDataSet('tree', 'tree', data.get(), ROOT.RooFit.Import(data))
            ROOT.SetOwnership(dset, False)
            data.Delete()
            f.Close()

            ff = TFile(output, 'update')
            logging.info('Saving data to {}'.format(output))
            tree = dset.tree()
            tree.Write('tree')
            #ws.Delete()
            dset.Delete()
            ff.Close()

def create_resamplers(options):
    import os.path
    import numpy as np
    import pickle
    from root_pandas import read_root
    from PIDPerfScripts.Binning import GetBinScheme

    pid_variables = ['{}_CombDLLK', '{}_CombDLLmu', '{}_CombDLLp', '{}_CombDLLe', '{}_V3ProbNNK', '{}_V3ProbNNpi', '{}_V3ProbNNmu', '{}_V3ProbNNp']
    kin_variables = ['{}_P', '{}_Eta','nTracks']


    with open('raw_data.json') as f:
        locations = json.load(f)
    if options.particles:
        locations =  [sample for sample in locations if sample["particle"] in options.particles]
    for sample in locations:
        binning_P = rooBinning_to_list(GetBinScheme(sample['branch_particle'], "P", None)) #last argument takes name of user-defined binning
        binning_ETA = rooBinning_to_list(GetBinScheme(sample['branch_particle'], "ETA", None)) #last argument takes name of user-defined binning TODO: let user pass this argument 
        binning_nTracks = rooBinning_to_list(GetBinScheme(sample['branch_particle'], "nTracks", None)) #last argument takes name of user-defined binning TODO: let user pass this argument
    	
        data = options.location + '/{particle}_Stripping{stripping}_Magnet{magnet}.root'.format(**sample)
        resampler_location = '{particle}_Stripping{stripping}_Magnet{magnet}.pkl'.format(**sample)
        if os.path.exists(resampler_location):
            os.remove(resampler_location)
        resamplers = dict()
        deps = map(lambda x: x.format(sample['branch_particle']), kin_variables)
        pids = map(lambda x: x.format(sample['branch_particle']), pid_variables)
        for pid in pids:
            if "DLL" in pid:
                target_binning = np.append([-1001], np.linspace(-150, 150, 300)) # binning for DLL
            elif "ProbNN" in pid:
                target_binning = np.linspace(0, 1, 100) # binning for ProbNN
            else:
                raise Exception
            resamplers[pid] = Resampler(binning_P, binning_ETA, binning_nTracks, target_binning)
        for i, chunk in enumerate(read_root(data, columns=deps + pids + ['nsig_sw'], chunksize=100000, where=options.cutstring)): # where is None if option is not set 
            for pid in pids:
                resamplers[pid].learn(chunk[deps + [pid]].values.T, weights=chunk['nsig_sw'])
            logging.info('Finished chunk {}'.format(i))
        with open(resampler_location, 'wb') as f:
            pickle.dump(resamplers, f)


def resample_branch(options):
    # maybe use pyroot here instead of root_pandas
    import pickle
    from root_pandas import read_root

    with open(options.configfile) as f:
        config = json.load(f)

    #load resamplers into config dictionary
    for task in config["tasks"]:
        with open(task["resampler_path"], 'rb') as f:
            resamplers = pickle.load(f)
            for pid in task["pids"]:
                try:
                    pid["resampler"] = resamplers[pid["kind"]]
                except KeyError:
                    logging.error("No resampler found for "+task["particle"]+" and "+pid["kind"]+".")
                    raise

    chunksize = 100000
    for i, chunk in enumerate(read_root(options.source_file, ignore=["*_COV_"], chunksize=chunksize)):
        for task in config["tasks"]:
            deps = chunk[task["features"]]
            for pid in task["pids"]:
                chunk[pid["name"]] = pid["resampler"].sample(deps.values.T)
        chunk.to_root(options.output_file, mode="a")
        logging.info('Processed {} entries'.format((i+1) * chunksize))


with open('raw_data.json') as configfile:
    locations = json.load(configfile)

particle_set = set([sample["particle"] for sample in locations])

parser = argparse.ArgumentParser()
subparsers = parser.add_subparsers()

grab = subparsers.add_parser('grab_data', help='Downloads PID calib data from EOS and saves it as NTuples')
grab.set_defaults(func=grab_data)
grab.add_argument('output', help="Directory where grabbed data is being stored.")
grab.add_argument('--particles', nargs='*', help="Optional subset of particles for which calibration data will be downloaded. Choose from "+", ".join(particle_set))

create = subparsers.add_parser('create_resamplers', help='Generates resampling histograms from NTuples')
create.set_defaults(func=create_resamplers)
create.add_argument("location", help="Directory where grab_data downloaded the .root - files.")
create.add_argument('--particles', nargs='*', help="Optional subset of particles for which calibration data will be downloaded. Choose from "+", ".join(particle_set))
create.add_argument('--cutstring', help="Optional cutstring. For example you can cut on the runNumber.")

resample = subparsers.add_parser('resample_branch', help='Uses histograms to add resampled PID branches to a dataset')
resample.set_defaults(func=resample_branch)
resample.add_argument("configfile")
resample.add_argument("source_file")
resample.add_argument("output_file")
resample.add_argument("--use-clonetree", dest='use_clonetree', action='store_true', default=False)

if __name__ == '__main__':
    options = parser.parse_args()
    options.func(options)
    