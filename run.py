#!/usr/bin/env python3
import json
import numpy
import copy
from numpy.linalg import LinAlgError
from collections import OrderedDict
import gzip
import pickle
from os import getcwd, path, pathsep, sep, environ, walk, devnull
from ast import literal_eval
import math
from optparse import OptionParser
import subprocess
import sys
import warnings


class UsefulInputFiles(object):
    """Class of input files to be opened by this routine.

    Attributes:
        metadata = Baseline metadata including common min/max for year range.
        meas_summary_data (string): High-level measure summary data.
        meas_compete_data (string): Contributing microsegment data needed
            for measure competition.
        active_measures (string): Measures that are active for the analysis.
        meas_engine_out (string): Measure output summaries.
        htcl_totals (string): Heating/cooling energy totals by climate zone,
            building type, and structure type.
    """

    def __init__(self):
        self.metadata = "metadata.json"
        # UNCOMMENT WITH ISSUE 188
        # self.metadata = "metadata_2017.json"
        self.meas_summary_data = \
            ("supporting_data", "ecm_prep.json")
        self.meas_compete_data = ("supporting_data", "ecm_competition_data")
        self.active_measures = "run_setup.json"
        self.meas_engine_out_ecms = ("results", "ecm_results.json")
        self.meas_engine_out_agg = ("results", "agg_results.json")
        self.htcl_totals = ("supporting_data", "stock_energy_tech_data",
                            "htcl_totals.json")


class UsefulVars(object):
    """Class of variables that are used globally across functions.

    Attributes:
        adopt_schemes (list): Possible consumer adoption scenarios.
        retro_rate (float): Rate at which existing stock is retrofitted.
        aeo_years (list) = Modeling time horizon.
        discount_rate (float): General rate to use in discounting cash flows.
        com_timeprefs (dict): Time preference premiums for commercial adopters.
        out_break_czones (OrderedDict): Maps measure climate zone names to
            the climate zone categories used in summarizing measure outputs.
        out_break_bldgtypes (OrderedDict): Maps measure building type names to
            the building sector categories used in summarizing measure outputs.
        out_break_enduses (OrderedDict): Maps measure end use names to
            the end use categories used in summarizing measure outputs.
    """

    def __init__(self, base_dir, handyfiles):
        self.adopt_schemes = ['Technical potential', 'Max adoption potential']
        self.retro_rate = 0.01
        # Load metadata including AEO year range
        with open(path.join(base_dir, handyfiles.metadata), 'r') as aeo_yrs:
            try:
                aeo_yrs = json.load(aeo_yrs)
            except ValueError as e:
                raise ValueError(
                    "Error reading in '" +
                    handyfiles.metadata + "': " + str(e)) from None
        # Set minimum AEO modeling year
        aeo_min = aeo_yrs["min year"]
        # Set maximum AEO modeling year
        aeo_max = aeo_yrs["max year"]
        # Derive time horizon from min/max years
        self.aeo_years = [
            str(i) for i in range(aeo_min, aeo_max + 1)]
        self.discount_rate = 0.07
        self.com_timeprefs = {
            "rates": [10.0, 1.0, 0.45, 0.25, 0.15, 0.065, 0.0],
            "distributions": {
                "heating": {
                    key: [0.265, 0.226, 0.196, 0.192, 0.105, 0.013, 0.003]
                    for key in self.aeo_years},
                "cooling": {
                    key: [0.264, 0.225, 0.193, 0.192, 0.106, 0.016, 0.004]
                    for key in self.aeo_years},
                "water heating": {
                    key: [0.263, 0.249, 0.212, 0.169, 0.097, 0.006, 0.004]
                    for key in self.aeo_years},
                "ventilation": {
                    key: [0.265, 0.226, 0.196, 0.192, 0.105, 0.013, 0.003]
                    for key in self.aeo_years},
                "cooking": {
                    key: [0.261, 0.248, 0.214, 0.171, 0.097, 0.005, 0.004]
                    for key in self.aeo_years},
                "lighting": {
                    key: [0.264, 0.225, 0.193, 0.193, 0.085, 0.013, 0.027]
                    for key in self.aeo_years},
                "refrigeration": {
                    key: [0.262, 0.248, 0.213, 0.170, 0.097, 0.006, 0.004]
                    for key in self.aeo_years}}}
        self.out_break_czones = OrderedDict([
            ('AIA CZ1', 'AIA_CZ1'), ('AIA CZ2', 'AIA_CZ2'),
            ('AIA CZ3', 'AIA_CZ3'), ('AIA CZ4', 'AIA_CZ4'),
            ('AIA CZ5', 'AIA_CZ5')])
        self.out_break_bldgtypes = OrderedDict([
            ('Residential (New)', [
                'new', 'single family home', 'multi family home',
                'mobile home']),
            ('Residential (Existing)', [
                'existing', 'single family home', 'multi family home',
                'mobile home'],),
            ('Commercial (New)', [
                'new', 'assembly', 'education', 'food sales',
                'food service', 'health care', 'mercantile/service',
                'lodging', 'large office', 'small office', 'warehouse',
                'other']),
            ('Commercial (Existing)', [
                'existing', 'assembly', 'education', 'food sales',
                'food service', 'health care', 'mercantile/service',
                'lodging', 'large office', 'small office', 'warehouse',
                'other'])])
        self.out_break_enduses = OrderedDict([
            ('Heating (Equip.)', ["heating", "secondary heating"]),
            ('Cooling (Equip.)', ["cooling"]),
            ('Envelope', ["heating", "secondary heating", "cooling"]),
            ('Ventilation', ["ventilation"]),
            ('Lighting', ["lighting"]),
            ('Water Heating', ["water heating"]),
            ('Refrigeration', ["refrigeration", "other"]),
            ('Computers and Electronics', [
                "PCs", "non-PC office equipment", "TVs", "computers"]),
            ('Other', [
                "cooking", "drying", "ceiling fan", "fans & pumps",
                "MELs", "other"])])


class Measure(object):
    """Class representing individual efficiency measures.

    Attributes:
        **kwargs: Arbitrary keyword arguments used to fill measure attributes
            from an input dictionary.
        update_results (dict): Flags markets, savings, and financial metric
            outputs that have yet to be finalized by the analysis engine.
        markets (dict): Data grouped by adoption scheme on:
            a) 'master_mseg': a measure's master market microsegments (stock,
               energy, carbon, cost),
            b) 'mseg_adjust': all microsegments that contribute to each master
               microsegment (required later for measure competition).
            c) 'mseg_out_break': master microsegment breakdowns by key
               variables (e.g., climate zone, building type, end use, etc.)
        savings (dict): Energy, carbon, and stock, energy, and carbon cost
            savings for measure over baseline technology case.
        portfolio_metrics (dict): Financial metrics relevant to assessing a
            large portfolio of efficiency measures (e.g., CCE, CCC).
        consumer_metrics (dict): Financial metrics relevant to the adoption
            decisions of individual consumers (e.g., NPV, IRR, payback).
    """

    def __init__(self, handyvars, **kwargs):
        # Read Measure object attributes from measures input JSON
        for key, value in kwargs.items():
            setattr(self, key, value)
        self.savings, self.portfolio_metrics, self.consumer_metrics = (
            {} for n in range(3))
        self.update_results = {
            "savings and portfolio metrics": {},
            "consumer metrics": True}
        # Convert any master market microsegment data formatted as lists to
        # numpy arrays
        self.convert_to_numpy(self.markets)
        for adopt_scheme in handyvars.adopt_schemes:
            # Initialize 'uncompeted' and 'competed' versions of
            # Measure markets (initially, they are identical)
            self.markets[adopt_scheme] = {
                "uncompeted": copy.deepcopy(self.markets[adopt_scheme]),
                "competed": copy.deepcopy(self.markets[adopt_scheme])}
            self.update_results["savings and portfolio metrics"][
                adopt_scheme] = {"uncompeted": True, "competed": True}
            self.savings[adopt_scheme] = {
                "uncompeted": {
                    "stock": {
                        "cost savings (total)": None,
                        "cost savings (annual)": None},
                    "energy": {
                        "savings (total)": None,
                        "savings (annual)": None,
                        "cost savings (total)": None,
                        "cost savings (annual)": None},
                    "carbon": {
                        "savings (total)": None,
                        "savings (annual)": None,
                        "cost savings (total)": None,
                        "cost savings (annual)": None}},
                "competed": {
                    "stock": {
                        "cost savings (total)": None,
                        "cost savings (annual)": None},
                    "energy": {
                        "savings (total)": None,
                        "savings (annual)": None,
                        "cost savings (total)": None,
                        "cost savings (annual)": None},
                    "carbon": {
                        "savings (total)": None,
                        "savings (annual)": None,
                        "cost savings (total)": None,
                        "cost savings (annual)": None}}}
            self.portfolio_metrics[adopt_scheme] = {
                "uncompeted": {
                    "cce": None,
                    "cce (w/ carbon cost benefits)": None,
                    "ccc": None,
                    "ccc (w/ energy cost benefits)": None},
                "competed": {
                    "cce": None,
                    "cce (w/ carbon cost benefits)": None,
                    "ccc": None,
                    "ccc (w/ energy cost benefits)": None}}
            self.consumer_metrics = {
                "anpv": {
                    "stock cost": {
                        "residential": None,
                        "commercial": None
                    },
                    "energy cost": {
                        "residential": None,
                        "commercial": None
                    },
                    "carbon cost": {
                        "residential": None,
                        "commercial": None
                    }},
                "irr (w/ energy costs)": None,
                "irr (w/ energy and carbon costs)": None,
                "payback (w/ energy costs)": None,
                "payback (w/ energy and carbon costs)": None}

    def convert_to_numpy(self, markets):
        """Convert terminal/leaf node lists in a dict to numpy arrays.

        Args:
            markets (dict): Input dict with possible lists at terminal/leaf
                nodes.
        """
        for k, i in markets.items():
            if isinstance(i, dict):
                self.convert_to_numpy(i)
            else:
                if isinstance(markets[k], list):
                    markets[k] = numpy.array(markets[k])


class Engine(object):
    """Class representing a collection of efficiency measures.

    Attributes:
        handyvars (object): Global variables useful across class methods.
        measures (list): List of active measure objects to be analyzed.
        output (OrderedDict): Summary results data for all active measures.
    """

    def __init__(self, handyvars, measure_objects):
        self.handyvars = handyvars
        self.measures = measure_objects
        self.output_ecms, self.output_all = (OrderedDict() for n in range(2))
        self.output_all["All ECMs"] = OrderedDict([
            ("Markets and Savings (Overall)", OrderedDict())])
        for adopt_scheme in self.handyvars.adopt_schemes:
            self.output_all["All ECMs"]["Markets and Savings (Overall)"][
                adopt_scheme] = OrderedDict()
        for m in self.measures:
            # Set measure climate zone, building sector, and end use
            # output category names for use in filtering and/or breaking
            # out results
            czones, bldgtypes, end_uses = ([] for n in range(3))
            # Find measure climate zone output categories
            for cz in self.handyvars.out_break_czones.items():
                if any([x in cz[1] for x in m.climate_zone]) and \
                        cz[0] not in czones:
                    czones.append(cz[0])
            # Find measure building sector output categories
            for bldg in self.handyvars.out_break_bldgtypes.items():
                if any([x in bldg[1] for x in m.bldg_type]) and \
                        bldg[0] not in bldgtypes:
                    bldgtypes.append(bldg[0])
            # Find measure end use output categories
            for euse in self.handyvars.out_break_enduses.items():
                # Find primary end use categories
                if any([x in euse[1] for x in m.end_use["primary"]]) and \
                        euse[0] not in end_uses:
                    # * Note: classify special freezers ECM case as
                    # 'Refrigeration'; classify 'supply' side heating/cooling
                    # ECMs as 'Heating (Equip.)'/'Cooling (Equip.)' and
                    # 'demand' side heating/cooling ECMs as 'Envelope'
                    if (euse[0] == "Refrigeration" and
                        ("refrigeration" in m.end_use["primary"] or
                         "freezers" in m.technology)) or (
                        euse[0] != "Refrigeration" and ((
                            euse[0] in ["Heating (Equip.)",
                                        "Cooling (Equip.)"] and
                            "supply" in m.technology_type["primary"]) or (
                            euse[0] == "Envelope" and "demand" in
                            m.technology_type["primary"]) or (euse[0] not in [
                                "Heating (Equip.)", "Cooling (Equip.)",
                                "Envelope"]))):
                        end_uses.append(euse[0])
                # Find secondary end use categories
                if m.end_use["secondary"] is not None and any([
                    x in m.end_use["secondary"] for x in [
                        "heating", "cooling"]]) and "Envelope" not in end_uses:
                    end_uses.append("Envelope")

            # Set measure climate zone(s), building sector(s), and end use(s)
            # as filter variables
            self.output_ecms[m.name] = OrderedDict([
                ("Filter Variables", OrderedDict([
                    ("Applicable Climate Zones", czones),
                    ("Applicable Building Classes", bldgtypes),
                    ("Applicable End Uses", end_uses)])),
                ("Markets and Savings (Overall)", OrderedDict()),
                ("Markets and Savings (by Category)", OrderedDict()),
                ("Financial Metrics", OrderedDict([
                    ("Portfolio Level", OrderedDict()),
                    ("Consumer Level", OrderedDict())]))])
            for adopt_scheme in self.handyvars.adopt_schemes:
                # Initialize measure overall markets and savings
                self.output_ecms[m.name]["Markets and Savings (Overall)"][
                    adopt_scheme] = OrderedDict()
                # Initialize measure markets and savings broken out by climate
                # zone, building sector, and end use categories
                self.output_ecms[m.name]["Markets and Savings (by Category)"][
                    adopt_scheme] = OrderedDict()
                # Initialize measure financial metrics
                self.output_ecms[m.name]["Financial Metrics"][
                    "Portfolio Level"][adopt_scheme] = OrderedDict()

    def calc_savings_metrics(self, adopt_scheme, comp_scheme):
        """Calculate and update measure savings and financial metrics.

        Notes:
            Given information on measure master microsegments for
            each projection year, determine capital, energy, and carbon
            cost savings; energy and carbon savings; and the net present
            value, internal rate of return, simple payback, cost of
            conserved energy, and cost of conserved carbon for the measure.

        Args:
            adopt_scheme (string): Assumed consumer adoption scenario.
            comp_scheme (string): Assumed measure competition scenario.
        """
        # Find all active measures that require savings updates
        measures_update = [m for m in self.measures if m.update_results[
            "savings and portfolio metrics"][
            adopt_scheme][comp_scheme] is True]

        # Update measure savings and associated financial metrics
        for m in measures_update:
            # Initialize energy/energy cost savings, carbon/
            # carbon cost savings, and dicts for financial metrics
            scostsave_tot, scostsave, esave_tot, esave, ecostsave_tot, \
                ecostsave, csave_tot, csave, ccostsave_tot, ccostsave, \
                stock_anpv_res, stock_anpv_com, energy_anpv_res, \
                energy_anpv_com, carb_anpv_res, carb_anpv_com, \
                irr_e, irr_ec, payback_e, payback_ec, cce, cce_bens, ccc, \
                ccc_bens = ({
                    yr: None for yr in self.handyvars.aeo_years} for
                    n in range(24))

            # Determine the total uncompeted measure/baseline capital
            # cost and total number of applicable baseline stock units,
            # used below to calculate incremental capital cost per unit
            # stock for the measure in each year

            # Total uncompeted baseline capital cost
            stock_meas_cost_tot = {
                yr: m.markets[adopt_scheme]["uncompeted"]["master_mseg"][
                    "cost"]["stock"]["total"]["efficient"][yr] for
                yr in self.handyvars.aeo_years}
            # Total uncompeted measure capital cost
            stock_base_cost_tot = {
                yr: m.markets[adopt_scheme]["uncompeted"]["master_mseg"][
                    "cost"]["stock"]["total"]["baseline"][yr] for
                yr in self.handyvars.aeo_years}
            # Total number of applicable stock units
            nunits_tot = {
                yr: m.markets[adopt_scheme]["uncompeted"]["master_mseg"][
                    "stock"]["total"]["all"][yr] for
                yr in self.handyvars.aeo_years}

            # Set measure master microsegments for the current adoption and
            # competition schemes
            markets = m.markets[adopt_scheme][comp_scheme]["master_mseg"]

            # Calculate measure capital cost savings, energy/carbon savings,
            # energy/carbon cost savings, and financial metrics for each
            # projection year
            for yr in self.handyvars.aeo_years:

                # Calculate per unit baseline capital cost and incremental
                # measure capital cost (used in financial metrics
                # calculations below); set these values to zero for
                # years in which total number of units is zero
                if nunits_tot[yr] != 0:
                    # Per unit baseline capital cost
                    scostbase = \
                        stock_base_cost_tot[yr] / nunits_tot[yr]
                    # Per unit measure incremental capital cost
                    scostmeas_delt = \
                        (stock_base_cost_tot[yr] -
                         stock_meas_cost_tot[yr]) / nunits_tot[yr]
                else:
                    scostbase, scost_save = (0 for n in range(2))

                # Calculate total annual energy/carbon and capital/energy/
                # carbon cost savings for the measure vs. baseline. Total
                # savings reflect the impact of all measure adoptions
                # simulated up until and including the current year
                esave_tot[yr] = \
                    markets["energy"]["total"]["baseline"][yr] - \
                    markets["energy"]["total"]["efficient"][yr]
                csave_tot[yr] = \
                    markets["carbon"]["total"]["baseline"][yr] - \
                    markets["carbon"]["total"]["efficient"][yr]
                scostsave_tot[yr] = \
                    markets["cost"]["stock"]["total"]["baseline"][yr] - \
                    markets["cost"]["stock"]["total"]["efficient"][yr]
                ecostsave_tot[yr] = \
                    markets["cost"]["energy"]["total"]["baseline"][yr] - \
                    markets["cost"]["energy"]["total"]["efficient"][yr]
                ccostsave_tot[yr] = \
                    markets["cost"]["carbon"]["total"]["baseline"][yr] - \
                    markets["cost"]["carbon"]["total"]["efficient"][yr]

                # Calculate the annual energy/carbon and capital/energy/carbon
                # cost savings for the measure vs. baseline. (Annual savings
                # will later be used in measure competition routines). Annual
                # savings reflect the impact of only the measure adoptions
                # that are new in the current year
                esave[yr] = \
                    markets["energy"]["competed"]["baseline"][yr] - \
                    markets["energy"]["competed"]["efficient"][yr]
                csave[yr] = \
                    markets["carbon"]["competed"]["baseline"][yr] - \
                    markets["carbon"]["competed"]["efficient"][yr]
                scostsave[yr] = markets[
                    "cost"]["stock"]["competed"]["baseline"][yr] - \
                    markets["cost"]["stock"]["competed"]["efficient"][yr]
                ecostsave[yr] = markets[
                    "cost"]["energy"]["competed"]["baseline"][yr] - \
                    markets["cost"]["energy"]["competed"]["efficient"][yr]
                ccostsave[yr] = markets[
                    "cost"]["carbon"]["competed"]["baseline"][yr] - \
                    markets["cost"]["carbon"]["competed"]["efficient"][yr]

                # Set the lifetime of the baseline technology for comparison
                # with measure lifetime
                life_base = markets["lifetime"]["baseline"][yr]
                # Ensure that baseline lifetime is at least 1 year
                if type(life_base) == numpy.ndarray and any(life_base) < 1:
                    life_base[numpy.where(life_base) < 1] = 1
                elif type(life_base) != numpy.ndarray and life_base < 1:
                    life_base = 1
                # Set lifetime of the measure
                life_meas = markets["lifetime"]["measure"]
                # Ensure that measure lifetime is at least 1 year
                if type(life_meas) == numpy.ndarray and any(life_meas) < 1:
                    life_meas[numpy.where(life_meas) < 1] = 1
                elif type(life_meas) != numpy.ndarray and life_meas < 1:
                    life_meas = 1

                # Calculate measure financial metrics

                # Create short name for number of captured measure stock units
                nunits_meas = markets["stock"]["total"]["measure"][yr]
                # If the total baseline stock is zero or no measure units
                # have been captured for a given year, set financial metrics
                # to 999
                if nunits_tot[yr] == 0 or (
                    type(nunits_meas) != numpy.ndarray and nunits_meas < 1 or
                        type(nunits_meas) == numpy.ndarray and all(
                            nunits_meas) < 1):
                    if yr == self.handyvars.aeo_years[0]:
                        stock_anpv_res[yr], energy_anpv_res[yr], \
                            carb_anpv_res[yr], stock_anpv_com[yr], \
                            energy_anpv_com[yr], carb_anpv_com[yr], \
                            irr_e[yr], irr_ec[yr], payback_e[yr], \
                            payback_ec[yr], cce[yr], cce_bens[yr], \
                            ccc[yr], ccc_bens[yr] = [999 for n in range(14)]
                    else:
                        yr_prev = str(int(yr) - 1)
                        stock_anpv_res[yr], energy_anpv_res[yr], \
                            carb_anpv_res[yr], stock_anpv_com[yr], \
                            energy_anpv_com[yr], carb_anpv_com[yr], \
                            irr_e[yr], irr_ec[yr], payback_e[yr], \
                            payback_ec[yr], cce[yr], cce_bens[yr], \
                            ccc[yr], ccc_bens[yr] = [x[yr_prev] for x in [
                                stock_anpv_res, energy_anpv_res, carb_anpv_res,
                                stock_anpv_com, energy_anpv_com, carb_anpv_com,
                                irr_e, irr_ec, payback_e, payback_ec, cce,
                                cce_bens, ccc, ccc_bens]]
                # Otherwise, check whether any financial metric calculation
                # inputs that can be arrays are in fact arrays
                elif any(type(x) == numpy.ndarray for x in [
                        scostmeas_delt, esave[yr], life_meas]):
                    # Make copies of the above stock, energy, carbon, and cost
                    # variables for possible further manipulation below before
                    # using as inputs to the "metric update" function
                    scostmeas_delt_tmp, esave_tmp, ecostsave_tmp, csave_tmp, \
                        ccostsave_tmp, life_meas_tmp = [
                            scostmeas_delt, esave_tot[yr], ecostsave_tot[yr],
                            csave_tot[yr], ccostsave_tot[yr], life_meas]

                    # Ensure consistency in length of all "metric_update"
                    # inputs that can be arrays

                    # Determine the length that any array inputs to
                    # "metric_update" should consistently have
                    len_arr = next((len(item) for item in [
                        scostmeas_delt, esave_tot[yr], life_meas] if
                        type(item) == numpy.ndarray), None)

                    # Ensure all array inputs to "metric_update" are of the
                    # above length

                    # Check incremental capital cost input
                    if type(scostmeas_delt_tmp) != numpy.ndarray:
                        scostmeas_delt_tmp = numpy.repeat(
                            scostmeas_delt_tmp, len_arr)
                    # Check energy/energy cost and carbon/cost savings inputs
                    if type(esave_tmp) != numpy.ndarray:
                        esave_tmp = numpy.repeat(esave_tmp, len_arr)
                        ecostsave_tmp = numpy.repeat(ecostsave_tmp, len_arr)
                        csave_tmp = numpy.repeat(csave_tmp, len_arr)
                        ccostsave_tmp = numpy.repeat(ccostsave_tmp, len_arr)
                    # Check measure lifetime input
                    if type(life_meas_tmp) != numpy.ndarray:
                        life_meas_tmp = numpy.repeat(life_meas_tmp, len_arr)

                    # Initialize numpy arrays for financial metrics outputs
                    stock_anpv_res[yr], energy_anpv_res[yr], \
                        carb_anpv_res[yr], stock_anpv_com[yr], \
                        energy_anpv_com[yr], carb_anpv_com[yr], \
                        irr_e[yr], irr_ec[yr], payback_e[yr], payback_ec[yr], \
                        cce[yr], cce_bens[yr], ccc[yr], ccc_bens[yr] = (
                            numpy.repeat(None, len(scostmeas_delt_tmp)) for
                            v in range(14))

                    # Run measure energy/carbon/cost savings and lifetime
                    # inputs through "metric_update" function to yield
                    # financial metric outputs. To handle inputs that are
                    # arrays, use a for loop to generate an output for each
                    # input array element one-by-one and append it to the
                    # appropriate output list. Note that lifetime float
                    # values are translated to integers, and all
                    # energy, carbon, and energy/carbon cost savings values
                    # are normalized by total applicable stock units
                    for x in range(0, len(scostmeas_delt_tmp)):
                        stock_anpv_res[yr][x], energy_anpv_res[yr][x], \
                            carb_anpv_res[yr][x], stock_anpv_com[yr][x], \
                            energy_anpv_com[yr][x], carb_anpv_com[yr][x], \
                            irr_e[yr][x], irr_ec[yr][x], \
                            payback_e[yr][x], payback_ec[yr][x], \
                            cce[yr][x], cce_bens[yr][x], ccc[yr][x], \
                            ccc_bens[yr][x] = self.metric_update(
                                m, int(round(life_base)),
                                int(round(life_meas_tmp[x])),
                                scostbase, scostmeas_delt_tmp[x],
                                esave_tmp[x] / nunits_tot[yr],
                                ecostsave_tmp[x] / nunits_tot[yr],
                                csave_tmp[x] / nunits_tot[yr],
                                ccostsave_tmp[x] / nunits_tot[yr])
                else:
                    # Run measure energy/carbon/cost savings and lifetime
                    # inputs through "metric_update" function to yield
                    # financial metric outputs. Note that lifetime float
                    # values are translated to integers, and all
                    # energy, carbon, and energy/carbon cost savings values
                    # are normalized by total applicable stock units
                    stock_anpv_res[yr], energy_anpv_res[yr], \
                        carb_anpv_res[yr], stock_anpv_com[yr], \
                        energy_anpv_com[yr], carb_anpv_com[yr], \
                        irr_e[yr], irr_ec[yr], payback_e[yr], payback_ec[yr], \
                        cce[yr], cce_bens[yr], ccc[yr], ccc_bens[yr] = \
                        self.metric_update(
                            m, int(round(life_base)),
                            int(round(life_meas)), scostbase, scostmeas_delt,
                            esave_tot[yr] / nunits_tot[yr],
                            ecostsave_tot[yr] / nunits_tot[yr],
                            csave_tot[yr] / nunits_tot[yr],
                            ccostsave_tot[yr] / nunits_tot[yr])

            # Record final measure savings figures and financial metrics

            # Set measure savings dict to update
            save = m.savings[adopt_scheme][comp_scheme]
            # Update capital cost savings
            save["stock"]["cost savings (total)"] = scostsave_tot
            save["stock"]["cost savings (annual)"] = scostsave
            # Update energy and energy cost savings
            save["energy"]["savings (total)"] = esave_tot
            save["energy"]["savings (annual)"] = esave
            save["energy"]["cost savings (total)"] = ecostsave_tot
            save["energy"]["cost savings (annual)"] = ecostsave
            # Update carbon and carbon cost savings
            save["carbon"]["savings (total)"] = csave_tot
            save["carbon"]["savings (annual)"] = csave
            save["carbon"]["cost savings (total)"] = ccostsave_tot
            save["carbon"]["cost savings (annual)"] = ccostsave

            # Set measure portfolio-level financial metrics dict to update
            metrics_port = m.portfolio_metrics[adopt_scheme][comp_scheme]
            # Update cost of conserved energy
            metrics_port["cce"] = cce
            metrics_port["cce (w/ carbon cost benefits)"] = cce_bens
            # Update cost of conserved carbon
            metrics_port["ccc"] = ccc
            metrics_port["ccc (w/ energy cost benefits)"] = ccc_bens

            # Set measure savings and portolio-level metrics for the current
            # adoption and competition schemes to finalized status
            m.update_results["savings and portfolio metrics"][
                adopt_scheme][comp_scheme] = False

            # Update measure consumer-level financial metrics if they have
            # not already been finalized (these metrics remain constant across
            # all consumer adoption and measure competition schemes)
            if m.update_results["consumer metrics"] is True:
                # Set measure consumer-level financial metrics dict to update
                metrics_consumer = m.consumer_metrics
                # Update annuity equivalent NPVs
                metrics_consumer["anpv"]["stock cost"]["residential"], \
                    metrics_consumer["anpv"]["stock cost"]["commercial"] = [
                        stock_anpv_res, stock_anpv_com]
                metrics_consumer["anpv"]["energy cost"]["residential"], \
                    metrics_consumer["anpv"]["energy cost"]["commercial"] = [
                        energy_anpv_res, energy_anpv_com]
                metrics_consumer["anpv"]["carbon cost"]["residential"], \
                    metrics_consumer["anpv"]["carbon cost"]["commercial"] = [
                        carb_anpv_res, carb_anpv_com]
                # Update internal rate of return
                metrics_consumer["irr (w/ energy costs)"] = irr_e
                metrics_consumer["irr (w/ energy and carbon costs)"] = irr_ec
                # Update payback period
                metrics_consumer["payback (w/ energy costs)"] = payback_e
                metrics_consumer["payback (w/ energy and carbon costs)"] = \
                    payback_ec

                # Set measure consumer-level metrics to finalized status
                m.update_results["consumer metrics"] = False

    def metric_update(self, m, life_base, life_meas, scost_base,
                      scost_meas_delt, esave, ecostsave, csave, ccostsave):
        """Calculate measure financial metrics for a given year.

        Notes:
            Calculate internal rate of return, simple payback, and cost of
            conserved energy/carbon from cash flows and energy/carbon
            savings across the measure lifetime. In the cash flows, represent
            the benefits of longer lifetimes for lighting equipment ECMs over
            comparable baseline technologies.

        Args:
            m (object): Measure object.
            nunits (int): Total competed baseline units in a given year.
            nunits_meas (int): Total competed units captured by measure in
                given year.
            life_base (float): Baseline technology lifetime.
            life_meas (float): Measure lifetime.
            scost_base (list): Per unit baseline capital cost in given year.
            scost_meas_delt (float): Per unit upfront capital
                cost for measure over baseline unit in given year.
            esave (list): Per unit annual energy savings over measure
                lifetime, starting in given year.
            ecostsave (list): Per unit annual energy cost savings over
                measure lifetime, starting in a given year.
            csave (list): Per unit annual avoided carbon emissions over
                measure lifetime, starting in given year.
            ccostsave (list): Per unit annual carbon cost savings over
                measure lifetime, starting in a given year.

        Returns:
            Consumer and portfolio-level financial metrics for the given
            measure cost savings inputs.
        """
        # Develop four initial cash flow scenarios over the measure life:
        # 1) Cash flows considering capital costs only
        # 2) Cash flows considering capital costs and energy costs
        # 3) Cash flows considering capital costs and carbon costs
        # 4) Cash flows considering capital, energy, and carbon costs

        # For lighting equipment ECMs only: determine when over the course of
        # the ECM lifetime (if at all) a cost gain is realized from an avoided
        # purchase of the baseline lighting technology due to longer measure
        # lifetime; store this information in a list of year indicators for
        # subsequent use below.  Example: an LED bulb lasts 30 years compared
        # to a baseline bulb's 10 years, meaning 3 purchases of the baseline
        # bulb would have occurred by the time the LED bulb has reached the
        # end of its life.
        added_stockcost_gain_yrs = []
        if (life_meas > life_base) and ("lighting" in m.end_use[
            "primary"]) and (m.measure_type == "full service") and (
                m.technology_type["primary"] == "supply"):
            for i in range(1, life_meas):
                if i % life_base == 0:
                    added_stockcost_gain_yrs.append(i - 1)

        # If the measure lifetime is less than 1 year, set it to 1 year
        # (a minimum for measure lifetime to work in below calculations)
        if life_meas < 1:
            life_meas = 1

        # Construct capital cost cash flows across measure life

        # Initialize capital cost cash flows with upfront capital cost
        cashflows_s = numpy.array(scost_meas_delt)

        for life_yr in range(0, life_meas):
            # Check whether an avoided cost of the baseline technology should
            # be added for given year; if not, set this term to zero
            if life_yr in added_stockcost_gain_yrs:
                scost_life = scost_base
            else:
                scost_life = 0

            # Add avoided capital costs and saved energy and carbon costs
            # as appropriate
            cashflows_s = numpy.append(cashflows_s, scost_life)

        # Construct complete energy and carbon cash flows across measure
        # lifetime. First term (reserved for initial investment) is zero.
        cashflows_e, cashflows_c = [numpy.append(0, [x] * life_meas)
                                    for x in [ecostsave, ccostsave]]

        # Calculate net present values (NPVs) using the above cashflows
        npv_s, npv_e, npv_c = [
            numpy.npv(self.handyvars.discount_rate, x) for x in [
                cashflows_s, cashflows_e, cashflows_c]]

        # Develop arrays of energy and carbon savings across measure
        # lifetime (for use in cost of conserved energy and carbon calcs).
        # First term (reserved for initial investment figure) is zero, and
        # each array is normalized by number of captured stock units
        esave_array = numpy.append(0, [esave] * life_meas)
        csave_array = numpy.append(0, [csave] * life_meas)

        # Calculate Net Present Value and annuity equivalent Net Present Value
        # of the above energy and carbon savings
        npv_esave = numpy.npv(self.handyvars.discount_rate, esave_array)
        npv_csave = numpy.npv(self.handyvars.discount_rate, csave_array)

        # Calculate portfolio-level financial metrics

        # Calculate cost of conserved energy w/ and w/o carbon cost savings
        # benefits. Restrict denominator values less than or equal to zero
        if npv_esave > 0:
            cce = (-npv_s / npv_esave)
            cce_bens = (-(npv_s + npv_c) / npv_esave)
        else:
            cce, cce_bens = [999 for n in range(2)]

        # Calculate cost of conserved carbon w/ and w/o energy cost savings
        # benefits. Restrict denominator values less than or equal to zero
        if npv_csave > 0:
            ccc = (-npv_s / (npv_csave * 1000000))
            ccc_bens = (-(npv_s + npv_e) / (npv_csave * 1000000))
        else:
            ccc, ccc_bens = [999 for n in range(2)]

        # Calculate consumer-level financial metrics

        # Only calculate consumer-level financial metrics once; do not
        # recalculate if already finalized
        if m.update_results["consumer metrics"] is True:
            # Calculate Annualized Net Present Value (ANPV) using the above
            # cashflows for later use in measure competition calculations. For
            # residential sector measures, ANPV is calculated using the
            # above NPVs, with a general discount rate applied.  For commerical
            # sector measures, ANPV is calculated using multiple discount rate
            # levels that reflect various degrees of risk tolerance observed
            # amongst commercial adopters.  These discount rate levels are
            # imported from the commercial AEO demand module data.

            # Populate ANPVs for residential sector
            # Check whether measure applies to residential sector
            if any([x in ["single family home", "multi family home",
                          "mobile home"] for x in m.bldg_type]):
                # Set ANPV values under general discount rate
                try:
                    anpv_s_res, anpv_e_res, anpv_c_res = [
                        numpy.pmt(
                            self.handyvars.discount_rate, life_meas, x) for
                        x in [npv_s, npv_e, npv_c]]
                    if any([not math.isfinite(x) for x in [
                            anpv_s_res, anpv_e_res, anpv_c_res]]):
                        raise(ValueError)
                except ValueError:
                    anpv_s_res, anpv_e_res, anpv_c_res = (
                        999 for n in range(3))
            # If measure does not apply to residential sector, set residential
            # ANPVs to 'None'
            else:
                anpv_s_res, anpv_e_res, anpv_c_res = (
                    None for n in range(3))

            # Populate ANPVs for commercial sector
            # Check whether measure applies to commercial sector
            if any([x not in ["single family home", "multi family home",
                              "mobile home"] for x in m.bldg_type]):
                anpv_s_com, anpv_e_com, anpv_c_com = (
                    {} for n in range(3))
                # Set ANPV values under 7 discount rate categories
                try:
                    for ind, tps in enumerate(
                            self.handyvars.com_timeprefs["rates"]):
                        anpv_s_com["rate " + str(ind + 1)],\
                            anpv_e_com["rate " + str(ind + 1)],\
                            anpv_c_com["rate " + str(ind + 1)] = \
                            [numpy.pmt(tps, life_meas, numpy.npv(tps, x))
                             for x in [cashflows_s, cashflows_e, cashflows_c]]
                        if any([not math.isfinite(x) for x in [
                                anpv_s_com["rate " + str(ind + 1)],
                                anpv_e_com["rate " + str(ind + 1)],
                                anpv_c_com["rate " + str(ind + 1)]]]):
                            raise(ValueError)
                except ValueError:
                    anpv_s_com, anpv_e_com, anpv_c_com = (
                        999 for n in range(3))
            # If measure does not apply to commercial sector, set commercial
            # ANPVs to 'None'
            else:
                anpv_s_com, anpv_e_com, anpv_c_com = (
                    None for n in range(3))

            # Calculate internal rate of return and simple payback for capital
            # + energy and capital + energy + carbon cash flows.  Use try/
            # except to handle cases where IRR/payback cannot be calculated

            # IRR and payback given capital + energy cash flows
            try:
                irr_e = numpy.irr(cashflows_s + cashflows_e)
                if not math.isfinite(irr_e):
                    raise(ValueError)
            except ValueError:
                irr_e = 999
            try:
                payback_e = self.payback(cashflows_s + cashflows_e)
            except (ValueError, LinAlgError):
                payback_e = 999
            # IRR and payback given capital + energy + carbon cash flows
            try:
                irr_ec = numpy.irr(cashflows_s + cashflows_e + cashflows_c)
                if not math.isfinite(irr_ec):
                    raise(ValueError)
            except ValueError:
                irr_ec = 999
            try:
                payback_ec = \
                    self.payback(cashflows_s + cashflows_e + cashflows_c)
            except (ValueError, LinAlgError):
                payback_ec = 999
        else:
            anpv_s_res, anpv_e_res, anpv_c_res, anpv_s_com, anpv_e_com, \
                anpv_c_com, irr_e, irr_ec, payback_e, payback_ec = (
                    None for n in range(10))

        # Return all updated economic metrics
        return anpv_s_res, anpv_e_res, anpv_c_res, anpv_s_com, anpv_e_com, \
            anpv_c_com, irr_e, irr_ec, payback_e, payback_ec, cce, cce_bens, \
            ccc, ccc_bens

    def payback(self, cashflows):
        """Calculate simple payback period.

        Notes:
            Calculate the simple payback period given an input list of
            cash flows, which may be uneven.

        Args:
            cashflows (list): Cash flows across measure lifetime.

        Returns:
            Simple payback period for the input cash flows.
        """
        # Separate initial investment and subsequent cash flows
        # from "cashflows" input; extend cashflows up until 100 years
        # out to ensure calculation of all paybacks under 100 years
        investment, cashflows = cashflows[0], list(
            cashflows[1:]) + [cashflows[-1]] * (100 - len(cashflows[1:]))
        # If initial investment is positive, payback = 0
        if investment >= 0:
            payback_val = 0
        else:
            # Find absolute value of initial investment to compare
            # subsequent cash flows against
            investment = abs(investment)
            # Initialize cumulative cashflow and # years tracking
            total, years, cumulative = 0, 0, []
            # Add to years and cumulative cashflow trackers while cumulative
            # cashflow < investment
            for cashflow in cashflows:
                total += cashflow
                if total < investment:
                    years += 1
                cumulative.append(total)
            # If investment pays back within the measure lifetime,
            # calculate this payback period in years
            if years < len(cashflows):
                a = years
                # Case where payback period < 1 year
                if (years - 1) < 0:
                    b = investment
                    c = cumulative[0]
                # Case where payback period >= 1 year
                else:
                    b = investment - cumulative[years - 1]
                    c = cumulative[years] - cumulative[years - 1]
                payback_val = a + (b / c)
            # If investment does not pay back within measure lifetime,
            # set payback period to artifically high number
            else:
                payback_val = 999

        # Return updated payback period value in years
        return payback_val

    def compete_measures(self, adopt_scheme, htcl_totals):
        """Compete and apportion total stock/energy/carbon/cost across measures.

        Notes:
            Adjust each competing measure's 'baseline' and 'efficient'
            energy/carbon/cost to reflect either a) direct competition between
            measures, or b) the indirect effects of measure competition.

        Args:
            adopt_scheme (string): Assumed consumer adoption scenario.
        """
        # Establish list of key chains and supporting competition data for all
        # stock/energy/carbon/cost microsegments that contribute to a measure's
        # total stock/energy/carbon/cost microsegments, across active measures
        mseg_keys, mkts_adj = ([] for n in range(2))
        for x in self.measures:
            mseg_keys.extend(x.markets[adopt_scheme]["competed"][
                "mseg_adjust"]["contributing mseg keys and values"].keys())
            mkts_adj.append(x.markets[adopt_scheme]["competed"]["mseg_adjust"])

        # Establish list of unique key chains in mseg_keys list above,
        # ensuring that all 'primary' microsegments (e.g., relating to direct
        # equipment replacement) are ordered and updated before 'secondary'
        # microsegments (e.g., relating to indirect effects of equipment
        # replacement, such as reduced waste heat from changes in lighting)
        msegs = sorted(numpy.unique(mseg_keys))

        # Initialize a dict used to store data on overlaps between supply-side
        # heating/cooling ECMs (e.g., HVAC equipment) and demand-side
        # heating/cooling ECMs (e.g., envelope). If the current set of ECMs
        # does not affect both supply-side and demand-side heating/cooling
        # markets, this dict is set to None
        if any(["supply" in x for x in msegs]) and \
           any(["demand" in x for x in msegs]):
            htcl_adj_data = {"supply": {}, "demand": {}}
        else:
            htcl_adj_data = None

        # Run through all unique contributing microsegments in the above list,
        # determining how the initial measure stock/energy/carbon/cost data
        # associated with each should be adjusted to reflect the effects of
        # measure competition
        for msu in msegs:

            # Determine the subset of measures that pertain to the current
            # contributing microsegment
            measures_adj = [self.measures[x] for x in range(
                0, len(self.measures)) if msu in mkts_adj[x][
                "contributing mseg keys and values"].keys()]
            # Create short name for all ECM competition data pertaining to
            # current contributing microsegment
            msu_mkts = [m.markets[adopt_scheme]["competed"][
                "mseg_adjust"]["contributing mseg keys and values"][msu] for
                m in measures_adj]

            # If the current contributing microsegment is of the 'primary'
            # type, directly compete the microsegment across applicable
            # measures
            if "primary" in msu:
                # If multiple measures are competing for the primary
                # microsegment, determine the market shares of each competing
                # measure and adjust primary stock/energy/carbon/cost
                # totals for each measure accordingly, using separate market
                # share modeling routines for residential/commercial sectors.
                if len(measures_adj) > 1 and any(x in msu for x in (
                        'single family home', 'multi family home',
                        'mobile home')):
                    self.compete_res_primary(measures_adj, msu, adopt_scheme)
                elif len(measures_adj) > 1 and all(x not in msu for x in (
                        'single family home', 'multi family home',
                        'mobile home')):
                    self.compete_com_primary(measures_adj, msu, adopt_scheme)
            # If the current contributing microsegment is of the 'secondary'
            # type, adjust the microsegment across applicable measures as
            # needed to reflect competition of associated primary
            # contributing microsegment(s) for each measure
            elif "secondary" in msu:
                # Determine the climate zone, building type, and structure type
                # needed to link the secondary microsegment and associated3
                # primary microsegment(s)
                cz_bldg_struct = literal_eval(msu)
                secnd_mseg_adjkey = str((
                    cz_bldg_struct[1], cz_bldg_struct[2], cz_bldg_struct[-1]))
                # Determine the subset of measures pertaining to the given
                # secondary microsegment that require total energy/carbon/cost
                # adjustments due to changes in associated primary
                # microsegment(s) (note that secondary microsegments do not
                # affect stock totals, only energy/carbon and associated costs)
                measures_adj_scnd = [self.measures[x] for x in range(
                    0, len(self.measures)) if self.measures[x] in
                    measures_adj and any(
                    [(y[1] > 0) for y in mkts_adj[x][
                        "secondary mseg adjustments"]["market share"][
                        "original energy (total captured)"][
                        secnd_mseg_adjkey].items()])]
                # If at least one applicable measure requires adjustments to
                # total secondary energy/carbon/cost, proceed with the
                # adjustment calculation
                if len(measures_adj_scnd) > 0:
                    self.secondary_adj(measures_adj_scnd, msu,
                                       secnd_mseg_adjkey, adopt_scheme)

            # For any contributing microsegment that pertains to heating or
            # cooling, record data needed for additional adjustments to remove
            # overlaps between the supply-side and demand-side of heating
            # and cooling energy (note that supply-side and demand-side heating
            # and cooling ECMs are not directly competed). NOTE: EXCLUDE
            # SECONDARY HEATING/COOLING MICROSEGMENTS FOR NOW UNTIL
            # REASONABLE APPROACH FOR ADJUSTING THESE IS IMPLEMENTED

            # Ensure the current contributing microsegment pertains to
            # heating or cooling (marked by 'supply' or 'demand' keys) and
            # that both supply and demand-side ECMs are present in the analysis
            if ('primary' in msu and
                ('supply' in msu or 'demand' in msu)) and \
                    htcl_adj_data is not None:
                htcl_adj_data = self.htcl_adj_rec(
                    htcl_adj_data, msu, msu_mkts, htcl_totals)

        # Once all direct competition is finished, remove all recorded
        # overlapping energy use and associated carbon/costs between
        # supply-side and demand-side heating and cooling ECMs, provided both
        # are present in the analysis
        if htcl_adj_data is not None:
            # Find the subset of ECMs that applies to heating and cooling
            measures_htcl_adj = [m for m in self.measures if any([
                z[0] in ["heating", "cooling", "secondary heating"] for
                z in m.end_use.values() if z is not None])]

            # Remove energy, carbon, and cost overlaps between supply-side and
            # demand-side heating/cooling ECMs
            self.htcl_adj(measures_htcl_adj, adopt_scheme, htcl_adj_data)

    def compete_res_primary(self, measures_adj, mseg_key, adopt_scheme):
        """Apportion stock/energy/carbon/cost across competing residential measures.

        Notes:
            Determine the shares of a given market microsegment that are
            captured by a series of residential efficiency measures that
            compete for this market microsegment.

        Args:
            measures_adj (list): Competing residential measure objects.
            mseg_key (string): Competed market microsegment information
                (mseg type->czone->bldg->fuel->end use->technology type
                 ->structure type).
            adopt_scheme (string): Assumed consumer adoption scenario.
        """
        # Initialize list of dicts that each store the annual market fractions
        # captured by competing measures; also initialize a dict that sums
        # market fractions by year across competing measures (used to normalize
        # the measure market fractions such that they all sum to 1)
        mkt_fracs = [{} for l in range(0, len(measures_adj))]
        mkt_fracs_tot = dict.fromkeys(self.handyvars.aeo_years, 0)

        # Loop through competing measures and calculate market shares for each
        # based on their annualized capital and operating costs.

        # Set abbreviated names for the dictionaries containing measure
        # capital and operating cost values, accessed further below

        # Annualized capital cost dictionary
        anpv_s_in = [m.consumer_metrics["anpv"]["stock cost"][
            "residential"] for m in measures_adj]
        # Annualized operating cost dictionary
        anpv_e_in = [m.consumer_metrics["anpv"]["energy cost"][
            "residential"] for m in measures_adj]

        # Find the year range in which at least one measure that applies
        # to the competed primary microsegment is on the market
        years_on_mkt_all = []
        [years_on_mkt_all.extend(x.yrs_on_mkt) for x in measures_adj]
        years_on_mkt_all = numpy.unique(years_on_mkt_all)

        # Set market entry years for all competing measures
        mkt_entry_yrs = [
            m.market_entry_year for m in measures_adj]

        # Loop through competing measures and calculate market shares for
        # each based on their annualized capital and operating costs
        for ind, m in enumerate(measures_adj):
            # Set measure markets and market adjustment information

            # Loop through all years in time horizon
            for yr in self.handyvars.aeo_years:
                # Ensure measure is on the market in given year
                if yr in m.yrs_on_mkt:
                    # Set measure capital and operating cost inputs. * Note:
                    # operating cost is set to just energy costs (for now), but
                    # could be expanded to include maintenance and carbon costs

                    # Set capital cost (handle as numpy array or point value)
                    if type(anpv_s_in[ind][yr]) == numpy.ndarray:
                        cap_cost = numpy.zeros(len(anpv_s_in[ind][yr]))
                        for i in range(0, len(anpv_s_in[ind][yr])):
                            cap_cost[i] = anpv_s_in[ind][yr][i]
                    else:
                        cap_cost = anpv_s_in[ind][yr]
                    # Set operating cost (handle as numpy array or point value)
                    if type(anpv_e_in[ind][yr]) == numpy.ndarray:
                        op_cost = numpy.zeros(len(anpv_e_in[ind][yr]))
                        for i in range(0, len(anpv_e_in[ind][yr])):
                            op_cost[i] = anpv_e_in[ind][yr][i]
                    else:
                        op_cost = anpv_e_in[ind][yr]

                    # Calculate measure market fraction using log-linear
                    # regression equation that takes capital/operating
                    # costs as inputs

                    # Calculate weighted sum of incremental capital and
                    # operating costs
                    sum_wt = cap_cost * m.markets[adopt_scheme]["competed"][
                        "mseg_adjust"]["competed choice parameters"][
                            str(mseg_key)]["b1"][yr] + op_cost * \
                        m.markets[adopt_scheme]["competed"]["mseg_adjust"][
                        "competed choice parameters"][
                            str(mseg_key)]["b2"][yr]
                    # Guard against cases with high weighted sums of
                    # incremental capital and operating costs
                    if type(sum_wt) != numpy.ndarray and sum_wt > 500:
                        sum_wt = 500
                    elif type(sum_wt) == numpy.ndarray and any([
                            x > 500 for x in sum_wt]):
                        sum_wt = [500 if x > 500 else x for x in sum_wt]

                    # Calculate market fraction
                    mkt_fracs[ind][yr] = numpy.exp(sum_wt)

                    # Add calculated market fraction to mkt fraction sum
                    mkt_fracs_tot[yr] = \
                        mkt_fracs_tot[yr] + mkt_fracs[ind][yr]

        # Loop through competing measures to normalize their calculated
        # market shares to the total market share sum; use normalized
        # market shares to make adjustments to each measure's master
        # microsegment values
        for ind, m in enumerate(measures_adj):
            # Set measure markets and market adjustment information
            # Establish starting energy/carbon/cost totals and current
            # contributing primary energy/carbon/cost information for measure
            mast, adj, mast_list_base, mast_list_eff, adj_list_eff, \
                adj_list_base = self.compete_adj_dicts(
                    m, mseg_key, adopt_scheme)
            # Calculate annual market share fraction for the measure and
            # adjust measure's master microsegment values accordingly
            for yr in self.handyvars.aeo_years:
                # Ensure measure is on the market in given year; if not,
                # the measure either splits the market with other
                # competing measures if none of those measures is on
                # the market either, or else has a market share of zero
                if yr in m.yrs_on_mkt:
                    mkt_fracs[ind][yr] = \
                        mkt_fracs[ind][yr] / mkt_fracs_tot[yr]
                elif yr not in years_on_mkt_all:
                    mkt_fracs[ind][yr] = 1 / len(measures_adj)
                else:
                    mkt_fracs[ind][yr] = 0

        # Check for competing ECMs that apply to but a fraction of the competed
        # market, and apportion the remaining fraction of this market across
        # the other competing ECMs
        added_sbmkt_fracs = self.find_added_sbmkt_fracs(
            mkt_fracs, measures_adj, mseg_key, adopt_scheme)

        # Find an overall ECM stock turnover rate for each year in the competed
        # time horizon, where the overall rate is an average across all ECMs

        # Initialize weighted average lifetime across all competing ECMs
        eff_life = 0
        # Add each competing ECM's lifetime weighted by its market share
        # to the overall weighted ECM lifetime
        for ind2, m in enumerate(measures_adj):
            eff_life += m.markets[adopt_scheme]["competed"][
                "master_mseg"]["lifetime"]["measure"] * mkt_fracs[ind2][yr]
        # Handle case where overall weighted ECM lifetime is an array
        if type(eff_life) == numpy.ndarray:
            # Initialize overall ECM turnover rate as zero for all years
            eff_turnover_rt = {yr: numpy.zeros(len(eff_life)) for
                               yr in self.handyvars.aeo_years}
        else:
            # Initialize overall ECM turnover rate as zero for all years
            eff_turnover_rt = {yr: 0 for yr in self.handyvars.aeo_years}

        # Loop through all years in the ECM competition time horizon
        for ind1, yr in enumerate(years_on_mkt_all):
            # Handle case where overall weighted ECM lifetime is an array
            if type(eff_life) == numpy.ndarray:
                # Loop through all elements in the weighted ECM lifetime array
                for i in range(0, len(eff_life)):
                    # Determine the future year in which the competed ECM stock
                    # from the current year will turn over, calculated as the
                    # current year being looped through plus the overall
                    # weighted ECM lifetime
                    future_eff_turnover_yr = ind1 + int(eff_life[i]) + 1
                    # If the future year calculated above is within the ECM
                    # competition time horizon, set ECM stock turnover rate for
                    # that future year as 1/weighted ECM lifetime for the
                    # current year plus the retrofit rate
                    if future_eff_turnover_yr < len(years_on_mkt_all):
                        eff_turnover_rt[years_on_mkt_all[
                            future_eff_turnover_yr]][i] = \
                            (1 / eff_life[i]) + self.handyvars.retro_rate
            # Handle case where overall weighted lifetime across all competing
            # ECMs is a point value
            else:
                # Determine the future year in which the competed ECM stock
                # from the current year will turn over, calculated as the
                # current year being looped through plus the overall
                # weighted ECM lifetime
                future_eff_turnover_yr = ind1 + int(eff_life)
                # If the future year calculated above is within the ECM
                # competition time horizon, set ECM stock turnover rate for
                # that future year as 1/weighted ECM lifetime for the current
                # year plus the retrofit rate
                if future_eff_turnover_yr < len(years_on_mkt_all):
                    eff_turnover_rt[years_on_mkt_all[
                        future_eff_turnover_yr]] = \
                        (1 / eff_life) + self.handyvars.retro_rate

        # Loop through competing measures and apply competed market shares
        # and gains from sub-market fractions to each ECM's total energy,
        # carbon, and cost impacts
        for ind, m in enumerate(measures_adj):
            # Set measure markets and market adjustment information
            # Establish starting energy/carbon/cost totals and current
            # contributing primary energy/carbon/cost information for measure
            mast, adj, mast_list_base, mast_list_eff, adj_list_eff, \
                adj_list_base = self.compete_adj_dicts(
                    m, mseg_key, adopt_scheme)

            # Find a baseline stock turnover rate for each year in the modeling
            # time horizon

            # Initialize the baseline turnover rate as 1/baseline lifetime
            # in each year plus the retrofit rate
            base_turnover_rt = {
                yr: (1 / adj["lifetime"]["baseline"][yr]) +
                self.handyvars.retro_rate for yr in self.handyvars.aeo_years}
            # Loop through all years in the modeling time horizon
            for ind1, yr in enumerate(self.handyvars.aeo_years):
                # Set baseline lifetime for the contributing microsegment
                base_life = adj["lifetime"]["baseline"][yr]
                # Determine the future year in which the baseline stock
                # from the current year will turn over, calculated as the
                # current year being looped through plus the baseline lifetime
                future_base_turnover_yr = ind1 + int(base_life)
                # If the future year calculated above is within the modeling
                # time horizon, set baseline stock turnover rate for that
                # future year as 1/baseline lifetime plus retrofit rate
                if future_base_turnover_yr < len(self.handyvars.aeo_years):
                    base_turnover_rt[self.handyvars.aeo_years[
                        future_base_turnover_yr]] = 1 / base_life + \
                        self.handyvars.retro_rate

            for yr in self.handyvars.aeo_years:
                # Make the adjustment to the measure's stock/energy/carbon/
                # cost totals based on its updated competed market share
                # and stock turnover rates
                self.compete_adj(
                    mkt_fracs[ind], added_sbmkt_fracs[ind], mast, adj,
                    mast_list_base, mast_list_eff, adj_list_eff, adj_list_base,
                    yr, mseg_key, m, adopt_scheme, mkt_entry_yrs,
                    base_turnover_rt, eff_turnover_rt)

    def compete_com_primary(self, measures_adj, mseg_key, adopt_scheme):
        """Apportion stock/energy/carbon/cost across competing commercial measures.

        Notes:
            Determines the shares of a given market microsegment that are
            captured by a series of commerical efficiency measures that
            compete for this market microsegment.

        Args:
            measures_adj (list): Competing commercial measure objects.
            mseg_key (string): Competed market microsegment information
                (mseg type->czone->bldg->fuel->end use->technology type
                 ->structure type).
            adopt_scheme (string): Assumed consumer adoption scenario.
        """
        # Initialize list of dicts that each store the annual market fractions
        # captured by competing measures; also initialize a dict that records
        # the total annualized capital + operating costs for each measure
        # and discount rate level (used to choose which measure is adopted
        # under each discount rate level)
        mkt_fracs = [{} for l in range(0, len(measures_adj))]
        tot_cost = [{} for l in range(0, len(measures_adj))]

        # Calculate the total annualized cost (capital + operating) needed to
        # determine market shares below

        # Set abbreviated names for the dictionaries containing measure
        # capital and operating cost values, accessed further below

        # Annualized stock cost dictionary
        anpv_s_in = [m.consumer_metrics["anpv"]["stock cost"]["commercial"] for
                     m in measures_adj]
        # Annualized operating cost dictionary
        anpv_e_in = [m.consumer_metrics["anpv"]["energy cost"][
            "commercial"] for m in measures_adj]

        # Find the year range in which at least one measure that applies
        # to the competed primary microsegment is on the market
        years_on_mkt_all = []
        [years_on_mkt_all.extend(x.yrs_on_mkt) for x in measures_adj]
        years_on_mkt_all = numpy.unique(years_on_mkt_all)

        # Set market entry years for all competing measures
        mkt_entry_yrs = [
            m.market_entry_year for m in measures_adj]

        # Initialize a flag that indicates whether any competing measures
        # have arrays of annualized capital and/or operating costs rather
        # than point values (resultant of distributions on measure inputs),
        # for each year in the range above
        length_array = numpy.repeat(0, len(self.handyvars.aeo_years))

        # Loop through all years in time horizon
        for ind_l, yr in enumerate(self.handyvars.aeo_years):
            # Determine whether any of the competing measures have
            # arrays of annualized capital and/or operating costs for
            # the given year; if so, find the array length. * Note: all
            # array lengths should be equal to the 'nsamples' variable
            # defined in 'ecm_prep.py'
            if any([type(x[yr]) == numpy.ndarray or
                    type(y[yr]) == numpy.ndarray for
                    x, y in zip(anpv_s_in, anpv_e_in)]) is True:
                length_array[ind_l] = next(
                    (len(x[yr]) or len(y[yr]) for x, y in
                     zip(anpv_s_in, anpv_e_in) if type(x[yr]) ==
                     numpy.ndarray or type(y[yr]) == numpy.ndarray),
                    length_array[ind_l])

        # Loop through competing measures and calculate market shares for
        # each based on their annualized capital and operating costs
        for ind, m in enumerate(measures_adj):
            # Set measure markets and market adjustment information
            # Loop through all years in time horizon
            for ind_l, yr in enumerate(self.handyvars.aeo_years):
                # Ensure measure is on the market in given year
                if yr in m.yrs_on_mkt:
                    # Set measure capital and operating cost inputs. * Note:
                    # operating cost is set to just energy costs (for now), but
                    # could be expanded to include maintenance and carbon costs

                    # Handle cases where capital and/or operating cost inputs
                    # are specified as arrays for at least one of the competing
                    # measures. In this case, the capital and operating costs
                    # for all measures must be formatted consistently as arrays
                    # of the same length
                    if length_array[ind_l] > 0:
                        cap_cost, op_cost = ([
                            {} for n in range(length_array[ind_l])] for
                            n in range(2))
                        for i in range(length_array[ind_l]):
                            # Set capital cost input array
                            if type(anpv_s_in[ind][yr]) == numpy.ndarray:
                                cap_cost[i] = anpv_s_in[ind][yr][i]
                            else:
                                cap_cost[i] = anpv_s_in[ind][yr]
                            # Set operating cost input array
                            if type(anpv_e_in[ind][yr]) == numpy.ndarray:
                                op_cost[i] = anpv_e_in[ind][yr][i]
                            else:
                                op_cost[i] = anpv_e_in[ind][yr]
                        # Sum capital and operating cost arrays and add to the
                        # total cost dict entry for the given measure
                        tot_cost[ind][yr] = [
                            [] for n in range(length_array[ind_l])]
                        for l in range(0, len(tot_cost[ind][yr])):
                            for dr in sorted(cap_cost[l].keys()):
                                tot_cost[ind][yr][l].append(
                                    cap_cost[l][dr] + op_cost[l][dr])
                    # Handle cases where capital and/or operating cost inputs
                    # are specified as point values for all competing measures
                    else:
                        # Set capital cost point value
                        cap_cost = anpv_s_in[ind][yr]
                        # Set operating cost point value
                        op_cost = anpv_e_in[ind][yr]
                        # Sum capital and opearting cost point values and add
                        # to the total cost dict entry for the given measure
                        tot_cost[ind][yr] = []
                        for dr in sorted(cap_cost.keys()):
                            tot_cost[ind][yr].append(
                                cap_cost[dr] + op_cost[dr])

        # Loop through competing measures and use total annualized capital
        # + operating costs to determine the overall share of the market
        # that is captured by each measure; use market shares to make
        # adjustments to each measure's master microsegment values
        for ind, m in enumerate(measures_adj):
            # Calculate annual market share fraction for the measure and
            # adjust measure's master microsegment values accordingly

            # Loop through all years in time horizon
            for ind_l, yr in enumerate(self.handyvars.aeo_years):
                # Ensure measure is on the market in given year; if not,
                # the measure either splits the market with other
                # competing measures if none of those measures is on
                # the market either, or else has a market share of zero
                if yr in m.yrs_on_mkt:
                    # Set the fractions of commericial adopters who fall into
                    # each discount rate category for this particular
                    # microsegment
                    mkt_dists = m.markets[adopt_scheme]["competed"][
                        "mseg_adjust"]["competed choice parameters"][
                            str(mseg_key)]["rate distribution"][yr]
                    # For each discount rate category, find which measure has
                    # the lowest annualized cost and assign that measure the
                    # share of commercial market adopters defined for that
                    # category above

                    # Handle cases where capital and/or operating cost inputs
                    # are specified as lists for at least one of the competing
                    # measures.
                    if length_array[ind_l] > 0:
                        mkt_fracs[ind][yr] = [
                            [] for n in range(length_array[ind_l])]
                        for l in range(length_array[ind_l]):
                            for ind2, dr in enumerate(tot_cost[ind][yr][l]):
                                # Find the lowest annualized cost for the given
                                # set of competing measures and discount bin
                                min_val = min([
                                    tot_cost[x][yr][l][ind2] for x in
                                    range(0, len(measures_adj)) if
                                    yr in tot_cost[x].keys()])
                                # Determine how many of the competing measures
                                # have the lowest annualized cost under
                                # the given discount rate bin
                                min_val_ecms = [
                                    x for x in range(0, len(measures_adj)) if
                                    yr in tot_cost[x].keys() and
                                    tot_cost[x][yr][l][ind2] == min_val]
                                # If the current measure has the lowest
                                # annualized cost, assign it the appropriate
                                # market share for the current discount rate
                                # category being looped through, divided by the
                                # total number of competing measures that share
                                # the lowest annualized cost
                                if tot_cost[ind][yr][l][ind2] == min_val:
                                    mkt_fracs[ind][yr][l].append(
                                        mkt_dists[ind2] / len(min_val_ecms))
                                # Otherwise, set its market share for that
                                # discount rate bin to zero
                                else:
                                    mkt_fracs[ind][yr][l].append(0)
                            mkt_fracs[ind][yr][l] = sum(
                                mkt_fracs[ind][yr][l])
                        # Convert market fractions list to numpy array for
                        # use in compete_adj function below
                        mkt_fracs[ind][yr] = numpy.array(
                            mkt_fracs[ind][yr])
                    # Handle cases where capital and/or operating cost inputs
                    # are specified as point values for all competing measures
                    else:
                        mkt_fracs[ind][yr] = []
                        for ind2, dr in enumerate(tot_cost[ind][yr]):
                            # Find the lowest annualized cost for the given
                            # set of competing measures and discount bin
                            min_val = min([
                                tot_cost[x][yr][ind2] for x in
                                range(0, len(measures_adj)) if
                                yr in tot_cost[x].keys()])
                            # Determine how many of the competing measures
                            # have the lowest annualized cost under
                            # the given discount rate bin
                            min_val_ecms = [
                                x for x in range(0, len(measures_adj)) if
                                yr in tot_cost[x].keys() and
                                tot_cost[x][yr][ind2] == min_val]
                            # If the current measure has the lowest
                            # annualized cost, assign it the appropriate
                            # market share for the current discount rate
                            # category being looped through, divided by the
                            # total number of competing measures that share
                            # the lowest annualized cost
                            if tot_cost[ind][yr][ind2] == min_val:
                                mkt_fracs[ind][yr].append(
                                    mkt_dists[ind2] / len(min_val_ecms))
                            # Otherwise, set its market share for that
                            # discount rate bin to zero
                            else:
                                mkt_fracs[ind][yr].append(0)
                        mkt_fracs[ind][yr] = sum(mkt_fracs[ind][yr])
                elif yr not in years_on_mkt_all:
                    mkt_fracs[ind][yr] = 1 / len(measures_adj)
                else:
                    mkt_fracs[ind][yr] = 0

        # Check for competing ECMs that apply to but a fraction of the competed
        # market, and apportion the remaining fraction of this market across
        # the other competing ECMs
        added_sbmkt_fracs = self.find_added_sbmkt_fracs(
            mkt_fracs, measures_adj, mseg_key, adopt_scheme)

        # Find an overall ECM stock turnover rate for each year in the competed
        # time horizon, where the overall rate is an average across all ECMs

        # Initialize weighted average lifetime across all competing ECMs
        eff_life = 0
        # Add each competing ECM's lifetime weighted by its market share
        # to the overall weighted ECM lifetime
        for ind2, m in enumerate(measures_adj):
            eff_life += m.markets[adopt_scheme]["competed"][
                "master_mseg"]["lifetime"]["measure"] * mkt_fracs[ind2][yr]
        # Handle case where overall weighted ECM lifetime is an array
        if type(eff_life) == numpy.ndarray:
            # Initialize overall ECM turnover rate as zero for all years
            eff_turnover_rt = {yr: numpy.zeros(len(eff_life)) for
                               yr in self.handyvars.aeo_years}
        else:
            # Initialize overall ECM turnover rate as zero for all years
            eff_turnover_rt = {yr: 0 for yr in self.handyvars.aeo_years}

        # Loop through all years in the ECM competition time horizon
        for ind1, yr in enumerate(years_on_mkt_all):
            # Handle case where overall weighted ECM lifetime is an array
            if type(eff_life) == numpy.ndarray:
                # Loop through all elements in the weighted ECM lifetime array
                for i in range(0, len(eff_life)):
                    # Determine the future year in which the competed ECM stock
                    # from the current year will turn over, calculated as the
                    # current year being looped through plus the overall
                    # weighted ECM lifetime
                    future_eff_turnover_yr = ind1 + int(eff_life[i])
                    # If the future year calculated above is within the ECM
                    # competition time horizon, set ECM stock turnover rate for
                    # that future year as 1/weighted ECM lifetime for the
                    # current year plus the retrofit rate
                    if future_eff_turnover_yr < len(years_on_mkt_all):
                        eff_turnover_rt[years_on_mkt_all[
                            future_eff_turnover_yr]][i] = \
                            (1 / eff_life[i]) + self.handyvars.retro_rate
            # Handle case where overall weighted lifetime across all competing
            # ECMs is a point value
            else:
                # Determine the future year in which the competed ECM stock
                # from the current year will turn over, calculated as the
                # current year being looped through plus the overall
                # weighted ECM lifetime
                future_eff_turnover_yr = ind1 + int(eff_life)
                # If the future year calculated above is within the ECM
                # competition time horizon, set ECM stock turnover rate for
                # that future year as 1/weighted ECM lifetime for the
                # current year plus the retrofit rate
                if future_eff_turnover_yr < len(years_on_mkt_all):
                    eff_turnover_rt[years_on_mkt_all[
                        future_eff_turnover_yr]] = \
                        (1 / eff_life) + self.handyvars.retro_rate

        # Loop through competing measures and apply competed market shares
        # and gains from sub-market fractions to each ECM's total energy,
        # carbon, and cost impacts
        for ind, m in enumerate(measures_adj):
            # Set measure markets and market adjustment information
            # Establish starting energy/carbon/cost totals and current
            # contributing primary energy/carbon/cost information for measure
            mast, adj, mast_list_base, mast_list_eff, adj_list_eff, \
                adj_list_base = self.compete_adj_dicts(
                    m, mseg_key, adopt_scheme)

            # Find a baseline stock turnover rate for each year in the modeling
            # time horizon

            # Initialize the baseline turnover rate as 1/baseline lifetime
            # in each year plus the retrofit rate
            base_turnover_rt = {
                yr: (1 / adj["lifetime"]["baseline"][yr]) +
                self.handyvars.retro_rate for yr in self.handyvars.aeo_years}
            # Loop through all years in the modeling time horizon
            for ind1, yr in enumerate(self.handyvars.aeo_years):
                # Set baseline lifetime for the contributing microsegment
                base_life = adj["lifetime"]["baseline"][yr]
                # Determine the future year in which the baseline stock
                # from the current year will turn over, calculated as the
                # current year being looped through plus the baseline lifetime
                future_base_turnover_yr = ind1 + int(base_life) + 1
                # If the future year calculated above is within the modeling
                # time horizon, set baseline stock turnover rate for that
                # future year as 1/baseline lifetime plus retrofit rate
                if future_base_turnover_yr < len(self.handyvars.aeo_years):
                    base_turnover_rt[self.handyvars.aeo_years[
                        future_base_turnover_yr]] = 1 / base_life + \
                        self.handyvars.retro_rate

            for yr in self.handyvars.aeo_years:
                # Make the adjustment to the measure's stock/energy/carbon/
                # cost totals based on its updated competed market share
                # and stock turnover rates
                self.compete_adj(
                    mkt_fracs[ind], added_sbmkt_fracs[ind], mast, adj,
                    mast_list_base, mast_list_eff, adj_list_eff, adj_list_base,
                    yr, mseg_key, m, adopt_scheme, mkt_entry_yrs,
                    base_turnover_rt, eff_turnover_rt)

    def find_added_sbmkt_fracs(
            self, mkt_fracs, measures_adj, mseg_key, adopt_scheme):
        """Add to competed ECM market shares to account for sub-market scaling.

        Notes:
            In cases where one or more competing ECM only applies to a fraction
            of its applicable baseline market (e.g., it is assigned with a
            sub-market scaling fraction/fractions), the fraction of the market
            that the ECM(s) does not apply to should be apportioned across the
            other competing ECMs and added to their competed market shares.
            This function determines the added fraction that should be added to
            each ECM's competed market share.

        Args:
            mkt_fracs (list): ECM market shares (before considering sub-mkts.).
            measures_adj (object): Competing ECM objects.
            mseg_key (string): Competed market microsegment information.
            adopt_scheme (string): Assumed consumer adoption scenario.

        Returns:
            Market fractions to add to each ECM's competed market share to
            reflect sub-market scaling in one or more competing ECMs.
        """
        # Set the fraction of the competed market that each ECM does not apply
        # to (to be apportioned across the other competing ECMs)
        noapply_sbmkt_fracs = [
            1 - m.markets[adopt_scheme]["competed"]["mseg_adjust"][
                "contributing mseg keys and values"][mseg_key][
                "sub-market scaling"] for m in measures_adj]

        # Determine the total weighted inapplicable fraction of the competed
        # market that must be apportioned across competing ECMs
        noapply_sbmkt_fracs_tot = {
            yr: sum([noapply_sbmkt_fracs[ind] * mkt_fracs[ind][yr] for
                     ind in range(len(measures_adj))]) for
            yr in self.handyvars.aeo_years}
        # Apportion the total inapplicable fraction across competing ECMs;
        # ensure that the apportionment removes each ECM's contribution to this
        # total fraction
        added_sbmkt_fracs = [
            {yr: ((noapply_sbmkt_fracs_tot[yr] -
             noapply_sbmkt_fracs[ind] * mkt_fracs[ind][yr]) /
             (len(measures_adj) - 1)) for yr in self.handyvars.aeo_years} for
            ind in range(0, len(measures_adj))]

        return added_sbmkt_fracs

    def secondary_adj(
            self, measures_adj, mseg_key, secnd_mseg_adjkey, adopt_scheme):
        """Adjust secondary microsegments to account for primary competition.

        Notes:
            Adjust a measure's secondary energy/carbon/cost totals to reflect
            the updated market shares calculated for an associated primary
            microsegment.

        Args:
            measures_adj (list): Competing commercial measure objects.
            mseg_key (string): Competed market microsegment information
                (mseg type->czone->bldg->fuel->end use->technology type
                 ->structure type).
            adopt_scheme (string): Assumed consumer adoption scenario.
        """
        # Loop through all measures that apply to the current contributing
        # secondary microsegment
        for ind, m in enumerate(measures_adj):
            # Establish starting energy/carbon/cost totals and current
            # contributing secondary energy/carbon/cost information for measure
            mast, adj, mast_list_base, mast_list_eff, adj_list_eff, \
                adj_list_base = self.compete_adj_dicts(
                    m, mseg_key, adopt_scheme)

            # Adjust secondary energy/carbon/cost totals based on the measure's
            # competed market share for an associated primary contributing
            # microsegment

            # Loop through all years where at least one measure that applies
            # to the secondary microsegment is on the market
            for yr in self.handyvars.aeo_years:
                # Find the appropriate market share adjustment information
                # for the given secondary climate zone, building type, and
                # structure type in the measure's 'mseg_adjust' attribute
                # and scale down the energy/carbon/cost totals accordingly
                secnd_adj_mktshr = m.markets[adopt_scheme]["competed"][
                    "mseg_adjust"]["secondary mseg adjustments"][
                    "market share"]
                # Calculate the competed and total market share adjustment
                # factors to apply to the measure secondary energy/carbon/cost
                # totals, where the 'competed' share considers the effects
                # of primary microsegment competition for the current year
                # only, and the 'total' share considers the effects of
                # primary microsegment competition for the current and all
                # previous years the measure was on the market

                # Set competed market share adjustment
                if secnd_adj_mktshr[
                        "original energy (competed and captured)"][
                        secnd_mseg_adjkey][yr] != 0:
                    adj_frac_comp = secnd_adj_mktshr[
                        "adjusted energy (competed and captured)"][
                        secnd_mseg_adjkey][yr] / secnd_adj_mktshr[
                        "original energy (competed and captured)"][
                        secnd_mseg_adjkey][yr]
                # Set competed market share adjustment to zero if total
                # originally captured baseline stock is zero for
                # current year
                else:
                    adj_frac_comp = 0

                # Set total market share adjustment
                if secnd_adj_mktshr["original energy (total captured)"][
                        secnd_mseg_adjkey][yr] != 0:
                    adj_frac_tot = secnd_adj_mktshr[
                        "adjusted energy (total captured)"][
                        secnd_mseg_adjkey][yr] / secnd_adj_mktshr[
                        "original energy (total captured)"][
                        secnd_mseg_adjkey][yr]
                # Set total market share adjustment to zero if total
                # originally captured baseline stock is zero for
                # current year
                else:
                    adj_frac_tot = 0

                # Adjust total and competed baseline and efficient
                # data by the appropriate secondary adjustment factor
                for x in ["baseline", "efficient"]:
                    # Determine appropriate adjustment data to use
                    # for baseline or efficient case
                    if x == "baseline":
                        mastlist, adjlist = [mast_list_base, adj_list_base]
                    else:
                        mastlist, adjlist = [mast_list_eff, adj_list_eff]
                    # Adjust the total and competed energy, carbon, and
                    # associated cost savings by the secondary adjustment
                    # factor, both overall and for the current
                    # contributing microsegment
                    mast["cost"]["energy"]["total"][x][yr], \
                        mast["cost"]["carbon"]["total"][x][yr], \
                        mast["energy"]["total"][x][yr], \
                        mast["carbon"]["total"][x][yr] = [
                            x[yr] - (y[yr] * (1 - adj_frac_tot)) for x, y in
                            zip(mastlist[1:5], adjlist[1:5])]
                    mast["cost"]["energy"]["competed"][x][yr], \
                        mast["cost"]["carbon"]["competed"][x][yr], \
                        mast["energy"]["competed"][x][yr], \
                        mast["carbon"]["competed"][x][yr] = [
                            x[yr] - (y[yr] * (1 - adj_frac_comp)) for x, y in
                            zip(mastlist[6:], adjlist[6:])]
                    adj["cost"]["energy"]["total"][x][yr], \
                        adj["cost"]["carbon"]["total"][x][yr], \
                        adj["energy"]["total"][x][yr], \
                        adj["carbon"]["total"][x][yr] = [
                            (x[yr] * adj_frac_tot) for x in adjlist[1:5]]
                    adj["cost"]["energy"]["competed"][x][yr], \
                        adj["cost"]["carbon"]["competed"][x][yr], \
                        adj["energy"]["competed"][x][yr], \
                        adj["carbon"]["competed"][x][yr] = [
                            (x[yr] * adj_frac_comp) for x in adjlist[6:]]

    def htcl_adj_rec(self, htcl_adj_data, msu, msu_mkts, htcl_totals):
        """Record overlaps in heating/cooling supply and demand-side energy.

        Args:
            htcl_adj_data (dict): Overlapping supply or demand-side heating/
                cooling energy use data to update with energy data for current
                contributing microsegment.
            msu (string): Current contributing microsegment key chain.
            msu_mkts (dict): Energy, carbon, and cost data for the current
                contributing microsegment, across all ECMs that apply to
                this microsegment.
            htcl_totals: Energy use totals for all possible climate zone,
                building type, and structure type combinations.

        Returns:
            Updated supply or demand-side heating/cooling overlap data that
            include overlapping energy use data from the current contributing
            microsegment.
        """
        # Establish criteria for matching a supply-side heating/
        # cooling microsegment with a demand-side heating/cooling
        # microsegment (same climate zone, building type, and structure
        # type)

        # Convert contributing microsegment key chain string to a list
        keys = literal_eval(msu)
        # Pull out climate zone, building type, structure type, fuel type,
        # and end use
        msu_split = [str(x) for x in [keys[1], keys[2], keys[-1],
                                      keys[3], keys[4]]]
        # Convert climate zone, building type, structure type, fuel type,
        # and end use data into a string, to be used as a dict key below
        msu_split_key = str(msu_split)
        # Set the technology type of the current heating/cooling microsegment
        # ('supply' or 'demand')
        tech_typ = keys[-3]

        # Determine whether overlapping heating/cooling energy use
        # data have already been initialized for the given climate
        # zone, building type, structure type, fuel type, and end use
        # combination; if not, initialize with overlapping energy use data for
        # all ECMs that apply to the current contributing microsegment; if
        # so, add the overlapping data to what is already there
        if msu_split_key not in htcl_adj_data[tech_typ].keys():
            htcl_adj_data[tech_typ][msu_split_key] = {
                # Record total potential overlapping supply-side
                # and demand-side heating/cooling energy use for
                # the given climate zone, building type,
                # structure type, fuel type, and end use combination
                "total": htcl_totals[msu_split[0]][msu_split[1]][
                    msu_split[2]][msu_split[3]][msu_split[4]],
                # Record the overlapping energy use that is actually
                # affected by the current contributing microsegment,
                # across all ECMs that apply to this microsegment
                "total affected": {yr: sum([(
                    m["energy"]["total"]["baseline"][yr]) for
                    m in msu_mkts]) for
                    yr in self.handyvars.aeo_years},
                # Record the savings in the overlapping energy use
                # affected by the current contributing microsegment,
                # across all ECMs that apply to this microsegment
                "affected savings": {yr: sum([(
                    m["energy"]["total"]["baseline"][yr] -
                    m["energy"]["total"]["efficient"][yr]) for
                    m in msu_mkts]) for yr in self.handyvars.aeo_years}}
        else:
            for yr in self.handyvars.aeo_years:
                # Add to affected overlapping energy use
                htcl_adj_data[tech_typ][msu_split_key][
                    "total affected"][yr] += sum([(
                        m["energy"]["total"]["baseline"][yr]) for
                        m in msu_mkts])
                # Add to affected overlapping energy use savings
                htcl_adj_data[tech_typ][msu_split_key][
                    "affected savings"][yr] += sum([(
                        m["energy"]["total"]["baseline"][yr] -
                        m["energy"]["total"]["efficient"][yr]) for
                        m in msu_mkts])

        return htcl_adj_data

    def htcl_adj(self, measures_htcl_adj, adopt_scheme, htcl_adj_data):
        """Remove heating/cooling supply-demand energy/carbon/cost overlaps.

        Notes:
            These additional adjustments are required to remove overlaps
            across supply-side and demand-side heating/cooling energy

        Args:
            measures_adj (list): Measures requiring supply-demand
                adjustments to energy/carbon/cost totals.
            adopt_scheme (string): Assumed consumer adoption scenario.
            htcl_adj_data (dict): Overlapping supply or demand-side heating/
                cooling energy use data to use in scaling down energy/carbon/
                cost overlaps.
        """
        # Loop through all ECMs requiring additional energy/carbon/cost
        # adjustments
        for m in measures_htcl_adj:
            # Determine the subset of ECM contributing microsegment keys that
            # apply to supply-side or demand-side heating/cooling. NOTE:
            # EXCLUDE SECONDARY HEATING/COOLING MICROSEGMENTS FOR NOW UNTIL
            # REASONABLE APPROACH FOR ADJUSTING THESE IS IMPLEMENTED
            htcl_keys = [k for k in m.markets[adopt_scheme]["competed"][
                "mseg_adjust"]["contributing mseg keys and values"].keys() if
                "primary" in k and ("supply" in k or "demand" in k)]
            # Loop through the ECM's supply-side or demand-side heating/cooling
            # contributing microsegments and scale down energy, carbon, and
            # cost data for that microsegment to remove previously recorded
            # overlaps across the heating/cooling supply-side and demand-side
            for mseg in htcl_keys:
                # Convert contributing microsegment key chain string to a list
                keys = literal_eval(mseg)
                # Pull out climate zone, building type, structure type,
                # fuel type, and end use
                msu_split = [str(x) for x in [keys[1], keys[2], keys[-1],
                                              keys[3], keys[4]]]
                # Convert climate zone, building type, structure type, fuel
                # type, and end use data into a string, to be used as a dict
                # key below
                msu_split_key = str(msu_split)
                # Set the technology type of the current microsegment, as well
                # as the technology types of overlapping microsegments (e.g.,
                # if the current microsegment is on the supply-side of
                # heating/cooling, overlapping microsegments are on the demand
                # side, and vice versa)
                if 'supply' in mseg:
                    tech_typ, tech_typ_overlp = ["supply", "demand"]
                else:
                    tech_typ, tech_typ_overlp = ["demand", "supply"]
                # If no overlapping energy use data exist for the current
                # microsegment's climate zone, building type, structure
                # type, fuel type, and end use combination, move to next
                # contributing microsegment
                if msu_split_key not in htcl_adj_data[tech_typ].keys():
                    continue

                # If overlapping energy use data do exist for the current
                # microsegment's climate zone, building type, structure
                # type, fuel type, and end use combination, create short name
                # for current microsegment energy data dict and overlapping
                # energy data dict; Note: if no overlapping energy data dict
                # can be found, move to next heating/cooling contributing
                # microsegment in for loop
                tech_data = htcl_adj_data[tech_typ][msu_split_key]
                try:
                    overlp_data = htcl_adj_data[tech_typ_overlp][msu_split_key]
                except KeyError:
                    continue
                # Establish set of dicts used to adjust the contributing
                # microsegment energy, carbon, and cost data and master energy,
                # carbon, and cost data to remove the overlaps
                mast, adj, mast_list_base, mast_list_eff, adj_list_eff, \
                    adj_list_base = self.compete_adj_dicts(
                        m, mseg, adopt_scheme)

                # Adjust contributing and master energy/carbon/cost
                # data to remove recorded supply-demand overlaps
                for yr in self.handyvars.aeo_years:
                    # Find the fraction of total possibly overlapping
                    # heating/cooling energy for the given climate zone,
                    # building type, and structure type combination that is
                    # actually affected by ECMs in the analysis (e.g., if
                    # looping through a supply-side contributing microsegment,
                    # this is the portion of total energy affected by demand-
                    # side microsegments in the analysis, and vice versa)
                    if overlp_data["total"][yr] != 0:
                        affected_frac = (overlp_data["total affected"][yr] /
                                         overlp_data["total"][yr])
                    else:
                        affected_frac = 0
                    # Find overall relative performance for the technology
                    # type of the current contributing microsegment in the
                    # given climate zone, building type, and structure type
                    # combination
                    if (type(tech_data["total affected"][yr]) !=
                        numpy.ndarray and
                        tech_data["total affected"][yr] != 0) or (
                        type(tech_data[
                            "total affected"][yr]) == numpy.ndarray and
                        all([x != 0 for x in
                             tech_data["total affected"][yr]])):
                        rel_perf_tech = (1 - (
                            tech_data["affected savings"][yr] /
                            tech_data["total affected"][yr]))
                    else:
                        rel_perf_tech = 1
                    # Find overall relative performance for the overlapping
                    # technology type in the given climate zone, building
                    # type, and structure type combination
                    if (type(overlp_data["total affected"][yr]) !=
                        numpy.ndarray and
                        overlp_data["total affected"][yr] != 0) or (
                        type(overlp_data[
                            "total affected"][yr]) == numpy.ndarray and
                        all([x != 0 for x in
                             overlp_data["total affected"][yr]])):
                        rel_perf_tech_overlp = (1 - (
                            overlp_data["affected savings"][yr] /
                            overlp_data["total affected"][yr]))
                    else:
                        rel_perf_tech_overlp = 1
                    # Calculate the ratio of relative performances between the
                    # current microsegment and overlapping microsegments'
                    # technology types in the given climate zone, building
                    # type, and structure type combination; ensure that
                    # neither performance value is negative for the comparison
                    if (all([type(x) != numpy.ndarray for x in [
                        rel_perf_tech, rel_perf_tech_overlp]]) and
                        (rel_perf_tech + rel_perf_tech_overlp) != 0) or (
                        any([type(x) == numpy.ndarray for x in [
                            rel_perf_tech, rel_perf_tech_overlp]]) and
                        all([x != 0 for x in (
                            rel_perf_tech + rel_perf_tech_overlp)])):
                        save_ratio = abs(rel_perf_tech) / (abs(
                            rel_perf_tech) + abs(rel_perf_tech_overlp))
                    else:
                        save_ratio = 1

                    # Use the affected overlap fraction, savings ratio, and
                    # overlapping relative performance calculated above to
                    # adjust the ECM's current contributing and master energy,
                    # carbon, and cost data and remove any overlaps
                    for x in ["baseline", "efficient"]:
                        # Determine appropriate adjustment data and factors to
                        # use for baseline or efficient case
                        if x == "baseline":
                            mastlist, adjlist = [mast_list_base, adj_list_base]
                            # Adjust baseline data to reflect the fraction of
                            # energy use affected by the overlapping
                            # microsegments, plus the portion of affected
                            # energy use saved by the overlapping microsegments
                            adj_frac = (1 - affected_frac) + \
                                affected_frac * (1 - save_ratio)
                        else:
                            mastlist, adjlist = [mast_list_eff, adj_list_eff]
                            # Adjust efficient data in the same way as
                            # baseline data, but with additional consideration
                            # for the energy savings benefits of the
                            # overlapping microsegments
                            adj_frac = (1 - affected_frac) + \
                                affected_frac * (1 - save_ratio) * \
                                rel_perf_tech_overlp
                        # Adjust the total and competed energy, carbon, and
                        # associated cost data for both the ECM's current
                        # contributing microsegment and master microsegment
                        mast["cost"]["energy"]["total"][x][yr], \
                            mast["cost"]["carbon"]["total"][x][yr], \
                            mast["energy"]["total"][x][yr],\
                            mast["carbon"]["total"][x][yr] = [
                                x[yr] - (y[yr] * (1 - adj_frac))
                                for x, y in zip(mastlist[1:5], adjlist[1:5])]
                        mast["cost"]["energy"]["competed"][x][yr], \
                            mast["cost"]["carbon"]["competed"][x][yr], \
                            mast["energy"]["competed"][x][yr],\
                            mast["carbon"]["competed"][x][yr] = [
                                x[yr] - (y[yr] * (1 - adj_frac))
                                for x, y in zip(mastlist[6:], adjlist[6:])]

    def compete_adj_dicts(self, m, mseg_key, adopt_scheme):
        """Set the initial measure market data needed to adjust for overlaps.

        Notes:
            Establishes a measure's initial stock/energy/carbon/cost totals
            (pre-competition) and the contributing microsegment information
            needed to adjust these totals following measure competition.

        Args:
            m (object): Measure needing market overlap adjustments.
            mseg_key (string): Key for competed market microsegment.
            adopt_scheme (string): Assumed consumer adoption scenario.

        Returns:
            Lists of initial measure master microsegment data and contributing
            microsegment data needed to adjust for competition across measures.
        """
        # Organize relevant starting master microsegment values into a list
        mast = m.markets[adopt_scheme]["competed"]["master_mseg"]
        # Set total-baseline and competed-baseline overall
        # stock/energy/carbon/cost totals to be updated in the
        # 'compete_adj', 'secondary_adj', and 'htcl_adj' functions
        mast_list_base = [
            mast["cost"]["stock"]["total"]["baseline"],
            mast["cost"]["energy"]["total"]["baseline"],
            mast["cost"]["carbon"]["total"]["baseline"],
            mast["energy"]["total"]["baseline"],
            mast["carbon"]["total"]["baseline"],
            mast["cost"]["stock"]["competed"]["baseline"],
            mast["cost"]["energy"]["competed"]["baseline"],
            mast["cost"]["carbon"]["competed"]["baseline"],
            mast["energy"]["competed"]["baseline"],
            mast["carbon"]["competed"]["baseline"]]
        # Set total-efficient and competed-efficient overall
        # stock/energy/carbon/cost totals to be updated in the
        # 'compete_adj', 'secondary_adj', and 'htcl_adj' functions
        mast_list_eff = [
            mast["cost"]["stock"]["total"]["efficient"],
            mast["cost"]["energy"]["total"]["efficient"],
            mast["cost"]["carbon"]["total"]["efficient"],
            mast["energy"]["total"]["efficient"],
            mast["carbon"]["total"]["efficient"],
            mast["cost"]["stock"]["competed"]["efficient"],
            mast["cost"]["energy"]["competed"]["efficient"],
            mast["cost"]["carbon"]["competed"]["efficient"],
            mast["energy"]["competed"]["efficient"],
            mast["carbon"]["competed"]["efficient"]]
        # Set up lists that will be used to determine the energy, carbon,
        # and cost totals associated with the contributing microsegment that
        # must be adjusted to reflect measure competition/interaction
        adj = m.markets[adopt_scheme]["competed"]["mseg_adjust"][
            "contributing mseg keys and values"][mseg_key]
        # Set total-baseline and competed-baseline contributing microsegment
        # stock/energy/carbon/cost totals to be updated in the
        # 'compete_adj', 'secondary_adj', and 'htcl_adj' functions
        adj_list_base = [
            adj["cost"]["stock"]["total"]["baseline"],
            adj["cost"]["energy"]["total"]["baseline"],
            adj["cost"]["carbon"]["total"]["baseline"],
            adj["energy"]["total"]["baseline"],
            adj["carbon"]["total"]["baseline"],
            adj["cost"]["stock"]["competed"]["baseline"],
            adj["cost"]["energy"]["competed"]["baseline"],
            adj["cost"]["carbon"]["competed"]["baseline"],
            adj["energy"]["competed"]["baseline"],
            adj["carbon"]["competed"]["baseline"]]
        # Set total-efficient and competed-efficient contributing microsegment
        # stock/energy/carbon/cost totals to be updated in the
        # 'compete_adj', 'secondary_adj', and 'htcl_adj' functions
        adj_list_eff = [
            adj["cost"]["stock"]["total"]["efficient"],
            adj["cost"]["energy"]["total"]["efficient"],
            adj["cost"]["carbon"]["total"]["efficient"],
            adj["energy"]["total"]["efficient"],
            adj["carbon"]["total"]["efficient"],
            adj["cost"]["stock"]["competed"]["efficient"],
            adj["cost"]["energy"]["competed"]["efficient"],
            adj["cost"]["carbon"]["competed"]["efficient"],
            adj["energy"]["competed"]["efficient"],
            adj["carbon"]["competed"]["efficient"]]

        return mast, adj, mast_list_base, mast_list_eff, adj_list_eff, \
            adj_list_base

    def compete_adj(
            self, adj_fracs, added_sbmkt_fracs, mast, adj, mast_list_base,
            mast_list_eff, adj_list_eff, adj_list_base, yr, mseg_key, measure,
            adopt_scheme, mkt_entry_yrs, base_turnover_rt, eff_turnover_rt):
        """Scale down measure stock/energy/carbon/cost totals to reflect competition.

        Notes:
            Scale stock/energy/carbon/cost totals associated with the current
            contributing market microsegment by the measure's market share for
            this microsegment; reflect these scaled down contributing
            microsegment totals in the measure's overall
            stock/energy/carbon/cost totals.

        Args:
            adj_fracs (dict): Competed market share(s) for the measure.
            added_sbmkt_fracs: Additional market share from sub-market scaling.
            mast (dict): Initial overall stock/energy/carbon/cost totals to
                adjust based on competed market share(s).
            adj (dict): Contributing microsegment data to use in adjusting
                overall stock/energy/carbon/cost following competition.
            mast_list_base (dict): Overall 'baseline' scenario
                stock/energy/carbon/cost totals.
            mast_list_eff (dict): Overall 'efficient' scenario
                stock/energy/carbon/cost totals.
            adj_list_eff (dict): Contributing microsegment 'efficient' scenario
                stock/energy/carbon/cost totals.
            adj_list_base (dict): Contributing microsegment 'baseline' scenario
                stock/energy/carbon/cost totals.
            yr (string): Current year in modeling time horizon.
            mseg_key (string): Key for competed market microsegment.
            measure (object): Measure needing competition adjustments.
            adopt_scheme (string): Assumed consumer adoption scenario.
            mkt_entry_yrs (list): Mkt. entry years for all competing measures.
            base_turnover_rt (dict): Baseline stock turnover rate by year.
            eff_turnover_rt (dict): ECM stock turnover rate by year.
        """
        # Set market shares for the competed stock in the current year, and
        # for the weighted combination of the competed stock for the current
        # and all previous years. Handle this calculation differently for
        # primary and secondary microsegment types

        # Set primary microsegment competed and total weighted market shares

        # Competed stock market share (represents adjustment for current
        # year)
        adj_frac_comp = adj_fracs[yr] + added_sbmkt_fracs[yr]

        # Weight the market share adjustment for the stock captured by the
        # measure in the current year against that of the stock captured
        # by the measure in all previous years, yielding a total weighted
        # market share adjustment

        if int(yr) < min(mkt_entry_yrs):
            adj_frac_tot = adj_fracs[yr] + added_sbmkt_fracs[yr]
        else:
            # Determine the subset of all years leading up to current year in
            # the modeling time horizon
            weighting_yrs = sorted([
                x for x in adj_fracs.keys() if
                (int(x) <= int(yr) and int(x) >= min(mkt_entry_yrs))])

            # Initialize previously captured efficient fraction and remaining
            # baseline stock fraction, used in the max adoption potential case
            eff_frac_map, base_frac_map = (0, 1)

            # Loop through the above set of years, successively updating the
            # weighted market share using a simple moving average
            for ind, wyr in enumerate(weighting_yrs):
                # First year in competed time horizon or any year in a
                # technical potential scenario; weighted market share equals
                # market share for the captured stock in this year only (a
                # "long run" market share value assuming 100% stock turnover)
                if ind == 0 or adopt_scheme == "Technical potential":
                    adj_frac_tot = (adj_fracs[wyr] + added_sbmkt_fracs[wyr])
                # Subsequent year for a max adoption potential scenario;
                # weighted market share averages market share for captured
                # stock in current year and all previous years
                else:
                    # All new stock is competed and captured by ECMs; the
                    # weight for the market share is thus the fraction of new
                    # stock added in the current year plus all new stock added
                    # since the beginning of the modeling time horizon
                    # (again, captured entirely by ECMs) that is up for
                    # replacement/retrofit
                    if "new" in mseg_key:
                        # Check that total new stock is not zero and that
                        # total new stock for the current year is greater
                        # than total new stock for the previous year (e.g.,
                        # that there have been new stock additions in the
                        # current year)
                        if adj["stock"]["total"]["all"][wyr] != 0 and \
                           (adj["stock"]["total"]["all"][wyr] > adj[
                                "stock"]["total"]["all"][str(int(wyr) - 1)]):
                            # Calculate the fraction of new stock that was
                            # added in the current year
                            new_add_wt = (
                                adj["stock"]["total"]["all"][wyr] - adj[
                                    "stock"]["total"]["all"][
                                    str(int(wyr) - 1)]) / adj[
                                "stock"]["total"]["all"][wyr]
                            # Calculate the fraction of all new stock
                            # previously captured by ECMs that is up for
                            # replacement/retrofit
                            eff_turnover_wt = (1 - new_add_wt) * \
                                eff_turnover_rt[wyr]
                            # Calculate the market share weight as the
                            # combination of all new stock added in the current
                            # year plus all new stock previously captured by
                            # ECMs that is up for replacement/retrofit
                            wt_comp = new_add_wt + eff_turnover_wt
                        # If total stock is zero or there were no new additions
                        # in the current year, exclude the current year from
                        # the market share weighting
                        else:
                            wt_comp = 0
                    # ECMs compete for the portion of the existing baseline
                    # stock that is up for replacement/retrofit plus all
                    # existing stock previously captured by ECMs that is up for
                    # replacement/retrofit
                    else:
                        # Calculate the portion of baseline stock that is up
                        # for replacement/retrofit; cap this portion by the
                        # portion of the total existing stock that remains with
                        # the comparable baseline technology
                        if base_turnover_rt[wyr] < base_frac_map:
                            base_turnover_wt = base_turnover_rt[wyr]
                        else:
                            base_turnover_wt = base_frac_map
                        # Calculate the portion of existing stock previously
                        # captured by ECMs that is up for replacement/retrofit
                        eff_turnover_wt = eff_turnover_rt[wyr] * eff_frac_map
                        # Calculate the market share weight as the
                        # combination of all existing baseline stock that is
                        # up for replacement/retrofit in the current year plus
                        # all existing stock previously captured by ECMs that
                        # is up for replacement/retrofit
                        wt_comp = base_turnover_wt + eff_turnover_wt

                    # Weighted market share equals the "long run" market share
                    # for the current year weighted by the fraction of the
                    # total market that is competed, plus any market share
                    # captured in previous years
                    adj_frac_tot = \
                        (1 - wt_comp) * adj_frac_tot + \
                        wt_comp * (adj_fracs[wyr] + added_sbmkt_fracs[wyr])

                # Update previously captured efficient fraction and
                # remaining baseline stock fraction, capping the
                # efficient fraction at 1
                if eff_frac_map + base_turnover_rt[wyr] < 1:
                    eff_frac_map += base_turnover_rt[wyr]
                    base_frac_map = 1 - eff_frac_map
                else:
                    eff_frac_map = 1
                    base_frac_map = 0

        # Ensure that total captured market share is never above 1
        if type(adj_frac_tot) != numpy.ndarray and adj_frac_tot > 1:
            adj_frac_tot = 1
        elif type(adj_frac_tot) == numpy.ndarray:
            adj_frac_tot[numpy.where(adj_frac_tot > 1)] = 1

        # For a primary microsegment with secondary effects, record market
        # share information that will subsequently be used to adjust associated
        # secondary microsegments and associated energy/carbon/cost totals
        if len(measure.markets[adopt_scheme]["competed"]["mseg_adjust"][
                "secondary mseg adjustments"]["market share"][
                "original energy (total captured)"].keys()) > 0:
            # Determine the climate zone, building type, and structure
            # type for the current contributing primary microsegment from the
            # microsegment key chain information and use as the key for linking
            # the primary and its associated secondary microsegment
            cz_bldg_struct = literal_eval(mseg_key)
            secnd_mseg_adjkey = str((
                cz_bldg_struct[1], cz_bldg_struct[2], cz_bldg_struct[-1]))

            if secnd_mseg_adjkey in measure.markets[adopt_scheme][
                "competed"]["mseg_adjust"][
                "secondary mseg adjustments"]["market share"][
                    "original energy (total captured)"].keys():
                # Record original and adjusted primary stock numbers as part of
                # the measure's 'mseg_adjust' attribute
                secnd_adj_mktshr = measure.markets[adopt_scheme][
                    "competed"]["mseg_adjust"]["secondary mseg adjustments"][
                    "market share"]
                # Total captured stock
                secnd_adj_mktshr["original energy (total captured)"][
                    secnd_mseg_adjkey][yr] += \
                    adj["energy"]["total"]["efficient"][yr]
                # Competed and captured stock
                secnd_adj_mktshr["original energy (competed and captured)"][
                    secnd_mseg_adjkey][yr] += \
                    adj["energy"]["competed"]["efficient"][yr]
                # Adjusted total captured stock
                secnd_adj_mktshr["adjusted energy (total captured)"][
                    secnd_mseg_adjkey][yr] += \
                    (adj["energy"]["total"]["efficient"][yr] * adj_frac_tot)
                # Adjusted competed and captured stock
                secnd_adj_mktshr["adjusted energy (competed and captured)"][
                    secnd_mseg_adjkey][yr] += (
                        adj["energy"]["competed"]["efficient"][yr] *
                        adj_frac_comp)

        # Adjust the total and competed stock captured by the measure by
        # the appropriate measure market share, both overall and for the
        # current contributing microsegment
        mast["stock"]["total"]["measure"][yr] = \
            mast["stock"]["total"]["measure"][yr] - \
            adj["stock"]["total"]["measure"][yr] * (1 - adj_frac_tot)
        mast["stock"]["competed"]["measure"][yr] = \
            mast["stock"]["competed"]["measure"][yr] - \
            adj["stock"]["competed"]["measure"][yr] * (1 - adj_frac_comp)
        adj["stock"]["total"]["measure"][yr] = \
            adj["stock"]["total"]["measure"][yr] * adj_frac_tot
        adj["stock"]["competed"]["measure"][yr] = \
            adj["stock"]["competed"]["measure"][yr] * adj_frac_comp

        # Adjust total and competed baseline and efficient data by measure
        # market share
        for x in ["baseline", "efficient"]:
            # Determine appropriate adjustment data to use
            # for baseline or efficient case
            if x == "baseline":
                mastlist, adjlist = [mast_list_base, adj_list_base]
            else:
                mastlist, adjlist = [mast_list_eff, adj_list_eff]

            # Adjust the total and competed energy, carbon, and associated cost
            # savings by the appropriate measure market share, both overall
            # and for the current contributing microsegment
            mast["cost"]["stock"]["total"][x][yr], \
                mast["cost"]["energy"]["total"][x][yr], \
                mast["cost"]["carbon"]["total"][x][yr], \
                mast["energy"]["total"][x][yr], \
                mast["carbon"]["total"][x][yr] = [
                    x[yr] - (y[yr] * (1 - adj_frac_tot)) for x, y in
                    zip(mastlist[0:5], adjlist[0:5])]
            mast["cost"]["stock"]["competed"][x][yr], \
                mast["cost"]["energy"]["competed"][x][yr], \
                mast["cost"]["carbon"]["competed"][x][yr], \
                mast["energy"]["competed"][x][yr], \
                mast["carbon"]["competed"][x][yr] = [
                    x[yr] - (y[yr] * (1 - adj_frac_comp)) for x, y in
                    zip(mastlist[5:], adjlist[5:])]
            adj["cost"]["stock"]["total"][x][yr], \
                adj["cost"]["energy"]["total"][x][yr], \
                adj["cost"]["carbon"]["total"][x][yr], \
                adj["energy"]["total"][x][yr], \
                adj["carbon"]["total"][x][yr] = [
                    (x[yr] * adj_frac_tot) for x in adjlist[0:5]]
            adj["cost"]["stock"]["competed"][x][yr], \
                adj["cost"]["energy"]["competed"][x][yr], \
                adj["cost"]["carbon"]["competed"][x][yr], \
                adj["energy"]["competed"][x][yr], \
                adj["carbon"]["competed"][x][yr] = [
                    (x[yr] * adj_frac_comp) for x in adjlist[5:]]

    def finalize_outputs(self, adopt_scheme):
        """Prepare selected measure outputs to write to a summary JSON file.

        Args:
            adopt_scheme (string): Consumer adoption scenario to summarize
                outputs for.
        """
        # Initialize markets and savings totals across all ECMs
        summary_vals_all_ecms = [{
            yr: 0 for yr in self.handyvars.aeo_years} for n in range(12)]
        # Set up subscript translator for carbon variable strings
        sub = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")
        # Loop through all measures and populate above dict of summary outputs
        for m in self.measures:
            # Set competed measure markets and savings; competed and uncompeted
            # portfolio-level financial metrics; and consumer-level
            # financial metrics
            mkts = m.markets[adopt_scheme]["competed"]["master_mseg"]
            save = m.savings[adopt_scheme]["competed"]
            metrics_port_uc = m.portfolio_metrics[adopt_scheme]["uncompeted"]
            metrics_port_c = m.portfolio_metrics[adopt_scheme]["competed"]
            metrics_consume = m.consumer_metrics

            # Group baseline/efficient markets, savings, and financial
            # metrics into list for updates
            summary_vals = [
                mkts["energy"]["total"]["baseline"],
                mkts["carbon"]["total"]["baseline"],
                mkts["cost"]["energy"]["total"]["baseline"],
                mkts["cost"]["carbon"]["total"]["baseline"],
                mkts["energy"]["total"]["efficient"],
                mkts["carbon"]["total"]["efficient"],
                mkts["cost"]["energy"]["total"]["efficient"],
                mkts["cost"]["carbon"]["total"]["efficient"],
                save["energy"]["savings (total)"],
                save["energy"]["cost savings (total)"],
                save["carbon"]["savings (total)"],
                save["carbon"]["cost savings (total)"],
                metrics_port_uc["cce"],
                metrics_port_uc["cce (w/ carbon cost benefits)"],
                metrics_port_uc["ccc"],
                metrics_port_uc["ccc (w/ energy cost benefits)"],
                metrics_port_c["cce"],
                metrics_port_c["cce (w/ carbon cost benefits)"],
                metrics_port_c["ccc"],
                metrics_port_c["ccc (w/ energy cost benefits)"],
                metrics_consume["irr (w/ energy costs)"],
                metrics_consume["irr (w/ energy and carbon costs)"],
                metrics_consume["payback (w/ energy costs)"],
                metrics_consume["payback (w/ energy and carbon costs)"]]
            # Order the year entries in the above markets, savings,
            # and portfolio metrics outputs
            summary_vals = [OrderedDict(
                sorted(x.items())) for x in summary_vals]
            # Add ECM markets and savings totals to totals across all ECMs
            summary_vals_all_ecms = [{
                yr: summary_vals_all_ecms[v][yr] + summary_vals[v][yr] for
                yr in self.handyvars.aeo_years} for v in range(0, 12)]

            # Find mean and 5th/95th percentile values of each output
            # (note: if output is point value, all three of these values
            # will be the same)

            # Mean of outputs
            energy_base_avg, carb_base_avg, energy_cost_base_avg, \
                carb_cost_base_avg, energy_eff_avg, carb_eff_avg, \
                energy_cost_eff_avg, carb_cost_eff_avg, energy_save_avg, \
                energy_costsave_avg, carb_save_avg, carb_costsave_avg, \
                cce_avg_uc, cce_c_avg_uc, ccc_avg_uc, ccc_e_avg_uc, \
                cce_avg_c, cce_c_avg_c, ccc_avg_c, ccc_e_avg_c, \
                irr_e_avg, irr_ec_avg, payback_e_avg, \
                payback_ec_avg = [{
                    k: numpy.mean(v) for k, v in z.items()} for
                    z in summary_vals]
            # 5th percentile of outputs
            energy_base_low, carb_base_low, energy_cost_base_low, \
                carb_cost_base_low, energy_eff_low, carb_eff_low, \
                energy_cost_eff_low, carb_cost_eff_low, energy_save_low, \
                energy_costsave_low, carb_save_low, carb_costsave_low, \
                cce_low_uc, cce_c_low_uc, ccc_low_uc, ccc_e_low_uc, \
                cce_low_c, cce_c_low_c, ccc_low_c, ccc_e_low_c, \
                irr_e_low, irr_ec_low, payback_e_low, payback_ec_low = [{
                    k: numpy.percentile(v, 5) for k, v in z.items()} for
                    z in summary_vals]
            # 95th percentile of outputs
            energy_base_high, carb_base_high, energy_cost_base_high, \
                carb_cost_base_high, energy_eff_high, carb_eff_high, \
                energy_cost_eff_high, carb_cost_eff_high, energy_save_high, \
                energy_costsave_high, carb_save_high, carb_costsave_high, \
                cce_high_uc, cce_c_high_uc, ccc_high_uc, ccc_e_high_uc, \
                cce_high_c, cce_c_high_c, ccc_high_c, ccc_e_high_c, \
                irr_e_high, irr_ec_high, payback_e_high, payback_ec_high = [{
                    k: numpy.percentile(v, 95) for k, v in z.items()} for
                    z in summary_vals]

            # Record updated markets and savings in Engine 'output'
            # attribute
            self.output_ecms[m.name]["Markets and Savings (Overall)"][
                adopt_scheme], self.output_ecms[m.name][
                    "Markets and Savings (by Category)"][
                    adopt_scheme] = (OrderedDict([
                        ("Baseline Energy Use (MMBtu)", energy_base_avg),
                        ("Efficient Energy Use (MMBtu)", energy_eff_avg),
                        ("Baseline CO2 Emissions (MMTons)".translate(sub),
                            carb_base_avg),
                        ("Efficient CO2 Emissions (MMTons)".translate(sub),
                            carb_eff_avg),
                        ("Baseline Energy Cost (USD)", energy_cost_base_avg),
                        ("Efficient Energy Cost (USD)", energy_cost_eff_avg),
                        ("Baseline CO2 Cost (USD)".translate(sub),
                            carb_cost_base_avg),
                        ("Efficient CO2 Cost (USD)".translate(sub),
                            carb_cost_eff_avg),
                        ("Energy Savings (MMBtu)", energy_save_avg),
                        ("Energy Cost Savings (USD)", energy_costsave_avg),
                        ("Avoided CO2 Emissions (MMTons)".
                            translate(sub), carb_save_avg),
                        ("CO2 Cost Savings (USD)".
                            translate(sub), carb_costsave_avg)]) for
                        n in range(2))

            # Scale down the measure's markets and savings by the
            # climate zone, building type, and end use partitioning
            # fractions previously established for the measure
            for k in self.output_ecms[m.name][
                "Markets and Savings (by Category)"][
                    adopt_scheme].keys():
                self.output_ecms[m.name][
                    'Markets and Savings (by Category)'][adopt_scheme][
                        k] = self.out_break_walk(
                        copy.deepcopy(m.markets[adopt_scheme][
                            "competed"]["mseg_out_break"]),
                        self.output_ecms[m.name][
                            'Markets and Savings (by Category)'][
                            adopt_scheme][k])

            # Record low and high estimates on markets, if available

            # Set shorter name for markets and savings output dict
            mkt_sv = self.output_ecms[m.name][
                "Markets and Savings (Overall)"][adopt_scheme]
            # Record low and high baseline market values
            if energy_base_avg != energy_base_low:
                # for x in [output_dict_overall, output_dict_bycat]:
                mkt_sv["Baseline Energy Use (low) (MMBtu)"] = energy_base_low
                mkt_sv["Baseline Energy Use (high) (MMBtu)"] = energy_base_high
                mkt_sv["Baseline CO2 Emissions (low) (MMTons)".
                       translate(sub)] = carb_base_low
                mkt_sv["Baseline CO2 Emissions (high) (MMTons)".
                       translate(sub)] = carb_base_high
                mkt_sv["Baseline Energy Cost (low) (USD)"] = \
                    energy_cost_base_low
                mkt_sv["Baseline Energy Cost (high) (USD)"] = \
                    energy_cost_base_high
                mkt_sv["Baseline CO2 Cost (low) (USD)".translate(sub)] = \
                    carb_cost_base_low
                mkt_sv["Baseline CO2 Cost (high) (USD)".translate(sub)] = \
                    carb_cost_base_high
            # Record low and high efficient market values
            if energy_eff_avg != energy_eff_low:
                # for x in [output_dict_overall, output_dict_bycat]:
                mkt_sv["Efficient Energy Use (low) (MMBtu)"] = energy_eff_low
                mkt_sv["Efficient Energy Use (high) (MMBtu)"] = energy_eff_high
                mkt_sv["Efficient CO2 Emissions (low) (MMTons)".
                       translate(sub)] = carb_eff_low
                mkt_sv["Efficient CO2 Emissions (high) (MMTons)".
                       translate(sub)] = carb_eff_high
                mkt_sv["Efficient Energy Cost (low) (USD)"] = \
                    energy_cost_eff_low
                mkt_sv["Efficient Energy Cost (high) (USD)"] = \
                    energy_cost_eff_high
                mkt_sv["Efficient CO2 Cost (low) (USD)".translate(sub)] = \
                    carb_cost_eff_low
                mkt_sv["Efficient CO2 Cost (high) (USD)".translate(sub)] = \
                    carb_cost_eff_high

            # Record updated portfolio metrics in Engine 'output' attribute;
            # yield low and high estimates on the metrics if available
            if cce_avg_uc != cce_low_uc:
                self.output_ecms[m.name]["Financial Metrics"][
                    "Portfolio Level"][adopt_scheme] = OrderedDict([
                        ("Cost of Conserved Energy (uncompeted) "
                         "($/MMBtu saved)", cce_avg_uc),
                        ("Cost of Conserved Energy (uncompeted) (low)"
                         "($/MMBtu saved)", cce_low_uc),
                        ("Cost of Conserved Energy (uncompeted) (high)"
                         "($/MMBtu saved)", cce_high_uc),
                        (("Cost of Conserved CO2 (uncompeted) "
                          "($/MTon CO2 avoided)").
                         translate(sub), ccc_avg_uc),
                        (("Cost of Conserved CO2 (uncompeted) (low) "
                          "($/MTon CO2 avoided)").
                         translate(sub), ccc_low_uc),
                        (("Cost of Conserved CO2 (uncompeted) (high) "
                          "($/MTon CO2 avoided)").
                         translate(sub), ccc_high_uc),
                        ("Cost of Conserved Energy ($/MMBtu saved)",
                            cce_avg_c),
                        ("Cost of Conserved Energy (low) ($/MMBtu saved)",
                            cce_low_c),
                        ("Cost of Conserved Energy (high) ($/MMBtu saved)",
                            cce_high_c),
                        (("Cost of Conserved CO2 "
                          "($/MTon CO2 avoided)").
                         translate(sub), ccc_avg_c),
                        (("Cost of Conserved CO2 (low) "
                          "($/MTon CO2 avoided)").
                         translate(sub), ccc_low_c),
                        (("Cost of Conserved CO2 (high) "
                          "($/MTon CO2 avoided)").
                         translate(sub), ccc_high_c)])
            else:
                self.output_ecms[m.name]["Financial Metrics"][
                    "Portfolio Level"][adopt_scheme] = OrderedDict([
                        ("Cost of Conserved Energy (uncompeted) "
                         "($/MMBtu saved)", cce_avg_uc),
                        (("Cost of Conserved CO2 (uncompeted) "
                          "($/MTon CO2 avoided)").
                         translate(sub), ccc_avg_uc),
                        ("Cost of Conserved Energy ($/MMBtu saved)",
                            cce_avg_c),
                        (("Cost of Conserved CO2 "
                          "($/MTon CO2 avoided)").
                         translate(sub), ccc_avg_c)])

            # Record updated consumer metrics in Engine 'output' attribute;
            # yield low and high estimates on the metrics if available
            if irr_e_avg != irr_e_low:
                self.output_ecms[m.name]["Financial Metrics"][
                    "Consumer Level"] = OrderedDict([
                        ("IRR (%)", irr_e_avg),
                        ("IRR (low) (%)", irr_e_low),
                        ("IRR (high) (%)", irr_e_high),
                        ("Payback (years)", payback_e_avg),
                        ("Payback (low) (years)", payback_e_low),
                        ("Payback (high) (years)", payback_e_high)])
            else:
                self.output_ecms[m.name]["Financial Metrics"][
                    "Consumer Level"] = OrderedDict([
                        ("IRR (%)", irr_e_avg),
                        ("Payback (years)", payback_e_avg)])

        # Find mean and 5th/95th percentile values of each market/savings
        # total across all ECMs (note: if total is point value, all three of
        # these values will be the same)

        # Mean of outputs across all ECMs
        energy_base_all_avg, carb_base_all_avg, energy_cost_base_all_avg, \
            carb_cost_base_all_avg, energy_eff_all_avg, carb_eff_all_avg, \
            energy_cost_eff_all_avg, carb_cost_eff_all_avg, \
            energy_save_all_avg, energy_costsave_all_avg, carb_save_all_avg, \
            carb_costsave_all_avg = [{
                k: numpy.mean(v) for k, v in z.items()} for
                z in summary_vals_all_ecms]
        # 5th percentile of outputs across all ECMs
        energy_base_all_low, carb_base_all_low, energy_cost_base_all_low, \
            carb_cost_base_all_low, energy_eff_all_low, carb_eff_all_low, \
            energy_cost_eff_all_low, carb_cost_eff_all_low, \
            energy_save_all_low, energy_costsave_all_low, carb_save_all_low, \
            carb_costsave_all_low = [{
                k: numpy.percentile(v, 5) for k, v in z.items()} for
                z in summary_vals_all_ecms]
        # 95th percentile of outputs across all ECMs
        energy_base_all_high, carb_base_all_high, energy_cost_base_all_high, \
            carb_cost_base_all_high, energy_eff_all_high, carb_eff_all_high, \
            energy_cost_eff_all_high, carb_cost_eff_all_high, \
            energy_save_all_high, energy_costsave_all_high, \
            carb_save_all_high, carb_costsave_all_high = [{
                k: numpy.percentile(v, 95) for k, v in z.items()} for
                z in summary_vals_all_ecms]

        # Record mean markets and savings across all ECMs
        self.output_all["All ECMs"]["Markets and Savings (Overall)"][
            adopt_scheme] = OrderedDict([
                ("Baseline Energy Use (MMBtu)", energy_base_all_avg),
                ("Baseline CO2 Emissions (MMTons)".translate(sub),
                 carb_base_all_avg),
                ("Baseline Energy Cost (USD)", energy_cost_base_all_avg),
                ("Baseline CO2 Cost (USD)".translate(sub),
                 carb_cost_base_all_avg),
                ("Energy Savings (MMBtu)", energy_save_all_avg),
                ("Energy Cost Savings (USD)", energy_costsave_all_avg),
                ("Avoided CO2 Emissions (MMTons)".translate(sub),
                 carb_save_all_avg),
                ("CO2 Cost Savings (USD)".translate(sub),
                 carb_costsave_all_avg),
                ("Efficient Energy Use (MMBtu)", energy_eff_all_avg),
                ("Efficient CO2 Emissions (MMTons)".translate(sub),
                 carb_eff_all_avg),
                ("Efficient Energy Cost (USD)", energy_cost_eff_all_avg),
                ("Efficient CO2 Cost (USD)".translate(sub),
                 carb_cost_eff_all_avg)])

        # Set shorter name for markets and savings output dict across all ECMs
        mkt_sv_all = self.output_all["All ECMs"][
            "Markets and Savings (Overall)"][adopt_scheme]

        # Record low/high estimates on efficient markets across all ECMs, if
        # available
        if energy_eff_all_avg != energy_eff_all_low:
            mkt_sv_all["Efficient Energy Use (low) (MMBtu)"] = \
                energy_eff_all_low
            mkt_sv_all["Efficient Energy Use (high) (MMBtu)"] = \
                energy_eff_all_high
            mkt_sv_all["Efficient CO2 Emissions (low) (MMTons)".
                       translate(sub)] = carb_eff_all_low
            mkt_sv_all["Efficient CO2 Emissions (high) (MMTons)".
                       translate(sub)] = carb_eff_all_high
            mkt_sv_all["Efficient Energy Cost (low) (USD)"] = \
                energy_cost_eff_all_low
            mkt_sv_all["Efficient Energy Cost (high) (USD)"] = \
                energy_cost_eff_all_high
            mkt_sv_all["Efficient CO2 Cost (low) (USD)".translate(sub)] = \
                carb_cost_eff_all_low
            mkt_sv_all["Efficient CO2 Cost (high) (USD)".translate(sub)] = \
                carb_cost_eff_all_high

    def out_break_walk(self, adjust_dict, adjust_vals):
        """Partition measure results by climate, building sector, and end use.

        Args:
            adjust_dict (dict): Results partitioning structure and fractions
                for climate zone, building sector, and end use.
            adjust_vals (dict): Unpartitioned energy, carbon, and cost
                markets/savings.

        Returns:
            Measure results partitioned by climate, building sector, and
            end use.
        """
        for (k, i) in sorted(adjust_dict.items()):
            if isinstance(i, dict):
                self.out_break_walk(i, adjust_vals)
            else:
                # Apply appropriate climate zone/building type/end use
                # partitioning fraction to the overall market/savings
                # value
                adjust_dict[k] = adjust_dict[k] * adjust_vals[k]

        return adjust_dict


def main(base_dir):
    """Import, finalize, and write out measure savings and financial metrics.

    Note:
        Import measures from a JSON, calculate competed and uncompeted
        savings and financial metrics for each measure, and write a summary
        of key results to an output JSON.
    """
    # Instantiate useful input files object
    handyfiles = UsefulInputFiles()
    # Instantiate useful variables object
    handyvars = UsefulVars(base_dir, handyfiles)

    # Import measure files
    with open(path.join(base_dir, *handyfiles.meas_summary_data), 'r') as mjs:
        try:
            meas_summary = json.load(mjs)
        except ValueError as e:
            raise ValueError(
                "Error reading in '" + handyfiles.meas_summary_data +
                "': " + str(e)) from None

    # Import list of all unique active measures
    with open(path.join(base_dir, handyfiles.active_measures), 'r') as am:
        try:
            active_meas_all = numpy.unique(json.load(am)["active"])
        except ValueError as e:
            raise ValueError(
                "Error reading in '" + handyfiles.active_measures +
                "': " + str(e)) from None
    print('ECM attributes data load complete')

    active_ecms_w_jsons = 0
    # Check that all ECM names included in the active list have a
    # matching ECM definition in ./ecm_definitions; warn users about ECMs
    # that do not have a matching ECM definition, which will be excluded
    for mn in active_meas_all:
        if mn not in [m["name"] for m in meas_summary]:
            print("WARNING: ECM '" + mn + "' in 'run_setup.json' active " +
                  "list does not match any of the ECM names found in " +
                  "./ecm_definitions JSONs and will not be simulated")
        else:
            active_ecms_w_jsons += 1

    # After verifying that there are active measures to simulate with
    # corresponding JSON definitions, loop through measures data in JSON,
    # initialize objects for all measures that are active and valid
    if active_ecms_w_jsons == 0:
        raise(ValueError("No active measures found; ensure that the " +
                         "'active' list in run_setup.json is not empty " +
                         "and that all active measure names match those " +
                         "found in the 'name' field for corresponding " +
                         "measure definitions in ./ecm_definitions"))
    else:
        measures_objlist = [
            Measure(handyvars, **m) for m in meas_summary if
            m["name"] in active_meas_all and m["remove"] is False]

    # Load and set competition data for active measure objects; suppress
    # new line if not in verbose mode ('Data load complete' is appended to
    # this message on the same line of the console upon data load completion)
    if options.verbose:
        print('Importing ECM competition data...')
    else:
        print('Importing ECM competition data...', end="", flush=True)

    for m in measures_objlist:
        # Assemble folder path for measure competition data
        meas_folder_name = path.join(*handyfiles.meas_compete_data)
        # Assemble file name for measure competition data
        meas_file_name = m.name + ".pkl.gz"
        with gzip.open(path.join(
                base_dir, meas_folder_name, meas_file_name), 'r') as zp:
            try:
                meas_comp_data = pickle.load(zp)
            except Exception as e:
                raise Exception(
                    "Error reading in competition data of " +
                    "ECM '" + m.name + "': " + str(e)) from None

        for adopt_scheme in handyvars.adopt_schemes:
            m.markets[adopt_scheme]["competed"]["mseg_adjust"] = \
                meas_comp_data[adopt_scheme]
        # Print data import message for each ECM if in verbose mode
        verboseprint("Imported ECM '" + m.name + "' competition data")

    # Import total absolute heating and cooling energy use data, used in
    # removing overlaps between supply-side and demand-side heating/cooling
    # ECMs in the analysis
    with open(path.join(base_dir, *handyfiles.htcl_totals), 'r') as msi:
        try:
            htcl_totals = json.load(msi)
        except ValueError as e:
            raise ValueError(
                "Error reading in '" +
                handyfiles.htcl_totals + "': " + str(e)) from None

    # Print message to console; if in verbose mode, print to new line,
    # otherwise append to existing message on the console
    if options.verbose:
        print('ECM competition data load complete')
    else:
        print('Data load complete')

    # Instantiate an Engine object using active measures list
    a_run = Engine(handyvars, measures_objlist)

    # Calculate uncompeted and competed measure savings and financial
    # metrics, and write key outputs to JSON file
    for adopt_scheme in handyvars.adopt_schemes:
        # Calculate each measure's uncompeted savings and metrics,
        # and print progress update to user
        print("Calculating uncompeted '" + adopt_scheme +
              "' savings/metrics...", end="", flush=True)
        a_run.calc_savings_metrics(adopt_scheme, "uncompeted")
        print("Calculations complete")
        # Update each measure's competed markets to reflect the
        # removal of savings overlaps with competing measures,
        # and print progress update to user
        print("Competing ECMs for '" + adopt_scheme + "' scenario...",
              end="", flush=True)
        a_run.compete_measures(adopt_scheme, htcl_totals)
        print("Competition complete")
        # Calculate each measure's competed measure savings and metrics
        # using updated competed markets, and print progress update to user
        print("Calculating competed '" + adopt_scheme +
              "' savings/metrics...", end="", flush=True)
        a_run.calc_savings_metrics(adopt_scheme, "competed")
        print("Calculations complete")
        # Write selected outputs to a summary JSON file for post-processing
        a_run.finalize_outputs(adopt_scheme)

    # Notify user that all analysis engine calculations are completed
    print("All calculations complete; writing output data...", end="",
          flush=True)
    # Write summary outputs for individual measures to a JSON
    with open(path.join(
            base_dir, *handyfiles.meas_engine_out_ecms), "w") as jso:
        json.dump(a_run.output_ecms, jso, indent=2)
    # Write summary outputs across all measures to a JSON
    with open(path.join(
            base_dir, *handyfiles.meas_engine_out_agg), "w") as jso:
        json.dump(a_run.output_all, jso, indent=2)
    print("Data writing complete")

    # Plot output data in R

    # Notify user that the output data are being plotted
    print('Plotting output data...', end="", flush=True)

    # Ensure the presence of R/Perl in Windows user PATH environment variable
    if sys.platform.startswith('win'):
        if "R-" not in environ["PATH"]:
            # Find the path to the user's Rscript.exe file
            lookfor, r_path = ("R-", None)
            for root, directory, files in walk(path.join("C:", sep)):
                if lookfor in root and "Rscript.exe" in files:
                    r_path = root
                    break
            # If Rscript.exe was not found, yield warning; else add to PATH
            if r_path is None:
                warnings.warn("R executable not found for plotting")
            else:
                environ["PATH"] += pathsep + r_path
        if all([x not in environ["PATH"] for x in ["perl", "Perl"]]):
            # Find the path to the user's perl.exe file
            lookfor, perl_path = (["Perl", "perl"], None)
            for root, directory, files in walk(path.join("C:", sep)):
                if any([x in root for x in lookfor]) and "perl.exe" in files:
                    perl_path = root
                    break
            # If perl.exe was not found, yield warning; else add to PATH
            if perl_path is None:
                warnings.warn(
                    "Perl executable not found for plot XLSX writing")
            else:
                environ["PATH"] += pathsep + perl_path
    # If user's operating system cannot be determined, yield warning message
    elif sys.platform == "unknown":
        warnings.warn("Could not determine OS for plotting routine")

    # Run R code

    # Set variable used to hide subprocess output
    FNULL = open(devnull, 'w')

    try:
        # Define shell command for R plotting function
        shell_command = 'Rscript ' + path.join(base_dir, 'plots_shell.R')
        # Execute R code
        subprocess.run(shell_command, shell=True, check=True,
                       stdout=FNULL, stderr=FNULL)
        # Notify user of plotting outcome if no error is thrown
        print("Plotting complete")
    except AttributeError:
        # If run module in subprocess throws AttributeError, try
        # subprocess.call() (used in Python versions before 3.5)
        try:
            # Execute R code
            subprocess.check_call(shell_command, shell=True,
                                  stdout=FNULL, stderr=FNULL)
            # Notify user of plotting outcome if no error is thrown
            print("Plotting complete")
        except subprocess.CalledProcessError:
            try:
                # Define shell command for R plotting function - handle 3.5 bug
                # in escaping spaces/apostrophes by adding --vanilla command
                # (recommended here: https://stackoverflow.com/questions/
                # 50028090/is-this-a-bug-in-r-3-5)
                shell_command = 'Rscript --vanilla ' + \
                    '"' + path.join(base_dir, 'plots_shell.R') + '"'
                # Execute R code
                subprocess.check_call(shell_command, shell=True)
                # Notify user of plotting outcome if no error is thrown
                print("Plotting complete")
            except subprocess.CalledProcessError as err:
                print("Plotting failed to complete: ", err)
    except subprocess.CalledProcessError:
        # Else if run module in subprocess throws any other type of error,
        # try handling a bug in R 3.5 where spaces/apostrophes in a directory
        # name are not escaped
        try:
            # Define shell command for R plotting function - handle 3.5 bug
            # in escaping spaces/apostrophes by adding --vanilla command
            # (recommended here: https://stackoverflow.com/questions/50028090/
            # is-this-a-bug-in-r-3-5)
            shell_command = 'Rscript --vanilla ' + \
                '"' + path.join(base_dir, 'plots_shell.R') + '"'
            # Execute R code
            subprocess.run(shell_command, shell=True, check=True)
            # Notify user of plotting outcome if no error is thrown
            print("Plotting complete")
        except subprocess.CalledProcessError as err:
            print("Plotting failed to complete: ", err)


if __name__ == '__main__':
    import time
    start_time = time.time()
    base_dir = getcwd()
    # Handle command line '-v' argument specifying verbose mode
    parser = OptionParser()
    parser.add_option("-v", action="store_true", dest="verbose",
                      help="print all warnings to stdout")
    (options, args) = parser.parse_args()
    # Set function that only prints message when in verbose mode
    verboseprint = print if options.verbose else lambda *a, **k: None
    main(base_dir)
    hours, rem = divmod(time.time() - start_time, 3600)
    minutes, seconds = divmod(rem, 60)
    print("--- Runtime: %s (HH:MM:SS.mm) ---" %
          "{:0>2}:{:0>2}:{:05.2f}".format(int(hours), int(minutes), seconds))
