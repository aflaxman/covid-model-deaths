from datetime import datetime, timedelta
import functools
import multiprocessing
import os
from pathlib import Path
import shutil
from typing import List, Tuple

from db_queries import get_location_metadata
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
import numpy as np
import pandas as pd

from covid_model_deaths.compare_moving_average import CompareAveragingModelDeaths
from covid_model_deaths.data import get_input_data, plot_crude_rates, DeathModelData
from covid_model_deaths.drawer import Drawer
from covid_model_deaths.impute_death_threshold import impute_death_threshold
import covid_model_deaths.globals as cmd_globals
from covid_model_deaths.globals import COLUMNS, LOCATIONS
from covid_model_deaths.moving_average import moving_average_predictions
from covid_model_deaths.social_distancing_cov import SocialDistCov
from covid_model_deaths.utilities import submit_curvefit, CompareModelDeaths


def run_us_model(input_data_version: str, peak_file: str, output_path: str, datestamp_label: str,
                 yesterday_draw_path: str, before_yesterday_draw_path: str, previous_average_draw_path: str) -> None:
    full_df = get_input_data('full_data', input_data_version)
    death_df = get_input_data('deaths', input_data_version)
    age_pop_df = get_input_data('age_pop', input_data_version)
    age_death_df = get_input_data('age_death', input_data_version)
    get_input_data('us_pops').to_csv(f'{output_path}/pops.csv', index=False)

    backcast_output_path = f'{output_path}/backcast_for_case_to_death.csv'
    threshold_dates_output_path = f'{output_path}/threshold_dates.csv'
    raw_draw_path = f'{output_path}/state_data.csv'
    model_type_path = f'{output_path}/state_models_used.csv'
    average_draw_path = f'{output_path}/past_avg_state_data.csv'

    plot_crude_rates(death_df, level='subnat')

    cases_and_backcast_deaths = make_cases_and_backcast_deaths(full_df, death_df, age_pop_df, age_death_df)
    cases_and_backcast_deaths.to_csv(backcast_output_path, index=False)

    in_us = cases_and_backcast_deaths[COLUMNS.country] == LOCATIONS.usa.name
    state_level = ~cases_and_backcast_deaths[COLUMNS.state].isnull()
    us_states = cases_and_backcast_deaths.loc[in_us & state_level, COLUMNS.state].unique().to_list()

    us_threshold_dates = impute_death_threshold(cases_and_backcast_deaths,
                                                location_list=us_states,
                                                ln_death_rate_threshold=cmd_globals.LN_MORTALITY_RATE_THRESHOLD)
    us_threshold_dates.to_csv(threshold_dates_output_path, index=False)

    us_date_mean_df = make_date_mean_df(us_threshold_dates)

    us_location_ids, us_location_names = get_us_location_ids_and_names(full_df)
    ensemble_dirs = setup_ensemble_dirs(output_path)

    submit_models(death_df, age_pop_df, age_death_df, us_date_mean_df,
                  us_location_ids, us_location_names, peak_file, output_path)

    in_us = full_df[COLUMNS.country] == LOCATIONS.usa.name
    state_level = ~full_df[COLUMNS.state].isnull()
    usa_obs_df = full_df[in_us & state_level]

    draw_dfs, past_draw_dfs, models_used, days, ensemble_draws_dfs = compile_draws(us_location_ids, us_location_names,
                                                                                   ensemble_dirs, usa_obs_df,
                                                                                   us_threshold_dates, age_pop_df)
    if 'location' not in models_used:
        raise ValueError('No location-specific draws used, must be using wrong tag')

    draw_df = pd.concat(draw_dfs)
    model_type_df = pd.DataFrame({
        'location': us_location_names,
        'model_used': models_used
    })
    draw_df.to_csv(raw_draw_path, index=False)
    model_type_df.to_csv(model_type_path, index=False)

    ensemble_path = make_and_save_draw_plots(output_path, us_location_ids, us_location_names,
                                             ensemble_draws_dfs, days, models_used)

    average_draw_df = average_draws(output_path, yesterday_draw_path, before_yesterday_draw_path)
    average_draw_df.to_csv(average_draw_path)

    # NO NEED TO DO THIS, FOR NOW
    # past_draw_df = pd.concat(past_draw_dfs)
    # avg_df = get_peak_date(past_draw_df, avg_df)

    moving_average_path = make_and_save_compare_average_plots(output_path, raw_draw_path, average_draw_path,
                                                              yesterday_draw_path, before_yesterday_draw_path)

    compare_to_previous_path = make_and_save_compare_to_previous_plots(output_path, average_draw_path,
                                                                       previous_average_draw_path)

    send_plots_to_diagnostics(datestamp_label, ensemble_path, moving_average_path, compare_to_previous_path)


def make_cases_and_backcast_deaths(full_df: pd.DataFrame, death_df: pd.DataFrame,
                                   age_pop_df: pd.DataFrame, age_death_df: pd.DataFrame) -> pd.DataFrame:
    location_ids = get_location_ids(full_df)

    backcast_deaths_df = backcast_deaths_parallel(location_ids, death_df, age_pop_df, age_death_df)

    full_df_columns = [COLUMNS.location_id, COLUMNS.state, COLUMNS.country, COLUMNS.date,
                       COLUMNS.confirmed, COLUMNS.confirmed_case_rate]
    cases_and_backcast_deaths = full_df[full_df_columns].merge(backcast_deaths_df, how='outer').reset_index(drop=True)

    country_level = cases_and_backcast_deaths[COLUMNS.state].isnull()
    cases_and_backcast_deaths.loc[country_level, COLUMNS.state] = cases_and_backcast_deaths[COLUMNS.country]
    cases_and_backcast_deaths[COLUMNS.location_id] = cases_and_backcast_deaths[COLUMNS.location_id].astype(int)

    return cases_and_backcast_deaths


def make_date_mean_df(threshold_dates: pd.DataFrame) -> pd.DataFrame:
    # get mean data from dataset
    date_draws = [i for i in threshold_dates.columns if i.startswith('death_date_draw_')]
    date_mean_df = threshold_dates.copy()
    date_mean_df[COLUMNS.threshold_date] = date_mean_df.apply(
        lambda x: datetime.strptime(date_mean(x[date_draws]).strftime('%Y-%m-%d'), '%Y-%m-%d'),
        axis=1
    )
    date_mean_df[COLUMNS.country] = LOCATIONS.usa.name
    date_mean_df = date_mean_df.rename(index=str, columns={COLUMNS.location_bad: COLUMNS.location})
    date_mean_df = date_mean_df[[COLUMNS.location, COLUMNS.country, COLUMNS.threshold_date]]
    return date_mean_df


def submit_models(death_df: pd.DataFrame, age_pop_df: pd.DataFrame,
                  age_death_df: pd.DataFrame, date_mean_df: pd.DataFrame,
                  location_ids: List[int], location_names: List[str],
                  peak_file: str, output_directory: str) -> None:
    # submit models
    n_scenarios = len(cmd_globals.COV_SETTINGS) * len(cmd_globals.KS)
    n_draws = [int(1000 / n_scenarios)] * n_scenarios
    n_draws[-1] = n_draws[-1] + 1000 - np.sum(n_draws)

    N = len(location_ids)
    i = 0
    nursing_home_locations = [LOCATIONS.life_care.name]
    for location_id, location_name in zip(location_ids, location_names):
        i += 1
        print(f'{i} / {N} locations')
        mod = DeathModelData(death_df, age_pop_df, age_death_df, location_id, 'threshold',
                             subnat=True, rate_threshold=cmd_globals.LN_MORTALITY_RATE_THRESHOLD)
        if location_name in nursing_home_locations:
            # save only nursing homes
            mod_df = mod.df.copy()
        else:
            # save only others
            mod_df = mod.df.loc[~mod.df[COLUMNS.location].isin(nursing_home_locations)].reset_index(drop=True)
        mod_df = mod_df.loc[~(mod_df[COLUMNS.deaths].isnull())].reset_index(drop=True)
        n_i = 0
        for cov_sort, weights in cmd_globals.COV_SETTINGS:
            for k in cmd_globals.KS:
                # drop back-cast for modeling file, but NOT for the social distancing covariate step
                model_out_dir = f'{output_directory}/model_data_{cov_sort}_{k}'
                mod_df.to_csv(f'{model_out_dir}/{location_name}.csv', index=False)
                sd_cov = SocialDistCov(mod_df, date_mean_df)
                sd_cov_df = sd_cov.get_cov_df(weights=weights, k=k)
                sd_cov_df.to_csv(f'{model_out_dir}/{location_name} covariate.csv', index=False)
                if not os.path.exists(f'{model_out_dir}/{location_name}'):
                    os.mkdir(f'{model_out_dir}/{location_name}')

                submit_curvefit(job_name=f'curve_model_{location_id}_{cov_sort}_{k}',
                                location_id=location_id,
                                model_location=location_name,
                                model_location_id=location_id,
                                python=shutil.which('python'),
                                data_file=f'{model_out_dir}/{location_name}.csv',
                                cov_file=f'{model_out_dir}/{location_name} covariate.csv',
                                peaked_file=f'{peak_file}.csv',
                                output_dir=f'{model_out_dir}/{location_name}',
                                n_draws=n_draws[n_i])


def compile_draws(location_ids: List[int], location_names: List[str], ensemble_dirs: List[str],
                  obs_df: pd.DataFrame, threshold_dates: pd.DataFrame, age_pop_df: pd.DataFrame
                  ) -> Tuple[List[pd.DataFrame], List[pd.DataFrame], List, List, List[pd.DataFrame]]:
    draw_dfs = []
    past_draw_dfs = []
    models_used = []
    days_ = []
    ensemble_draws_dfs = []
    for location_id, location_name in zip(location_ids, location_names):
        # get draws
        data_draws = Drawer(
            ensemble_dirs=ensemble_dirs,
            location_name=location_name,
            location_id=int(location_id),
            obs_df=obs_df.loc[obs_df[COLUMNS.state] == location_name],
            date_draws=threshold_dates.loc[threshold_dates[COLUMNS.location_bad] == location_name,
                                           [i for i in threshold_dates.columns if i.startswith('death_date_draw_')]].values,
            population=age_pop_df.loc[age_pop_df[COLUMNS.location_id] == int(location_id), COLUMNS.population].sum()
        )
        draw_df, past_draw_df, model_used, days, ensemble_draws = data_draws.get_dated_draws()
        draw_dfs.append(draw_df)
        past_draw_dfs.append(past_draw_df)
        models_used.append(model_used)
        days_.append(days)
        ensemble_draws_dfs.append(ensemble_draws)
    return draw_dfs, past_draw_dfs, models_used, days_, ensemble_draws_dfs


def average_draws(output_dir: str, yesterday_path: str, before_yesterday_path: str) -> pd.DataFrame:
    raw_draw_path = f'{output_dir}/state_data.csv'
    avg_df = moving_average_predictions(
        'US',
        specified=True,
        model_1=raw_draw_path,
        model_2=yesterday_path,
        model_3=before_yesterday_path,
    )
    avg_df['date'] = pd.to_datetime(avg_df['date'])
    return avg_df


def make_and_save_draw_plots(output_dir: str, location_ids: List[int], location_names: List[str],
                             ensemble_draw_dfs: List[pd.DataFrame], days_: List, models_used: List) -> str:
    # plot ensemble
    # ensemble plot settings
    color_dict = {
        'equal': 'gold',
        'ascmid': 'firebrick',
        'ascmax': 'darkviolet'
    }
    line_dict = {
        '21': '--'
    }
    # HACK: Pulled out of a notebook that relied on this only having one item.
    k = cmd_globals.KS[0]
    plot_vars = zip(location_ids, location_names, ensemble_draw_dfs, days_, models_used)
    plot_path = f'{output_dir}/ensemble_plot.pdf'
    with PdfPages(plot_path) as pdf:
        for location_id, location_name, ensemble_draws, days, model_used in plot_vars:
            fig, ax = plt.subplots(1, 2, figsize=(11, 8.5))
            for label, draws in ensemble_draws.items():
                label = label.split('model_data_')[1]
                draws = np.exp(draws)
                deaths_mean = draws.mean(axis=0)
                deaths_lower = np.percentile(draws, 2.5, axis=0)
                deaths_upper = np.percentile(draws, 97.5, axis=0)

                d_deaths_mean = (draws[:, 1:] - draws[:, :-1]).mean(axis=0)
                d_deaths_lower = np.percentile(draws[:, 1:] - draws[:, :-1], 2.5, axis=0)
                d_deaths_upper = np.percentile(draws[:, 1:] - draws[:, :-1], 97.5, axis=0)

                # cumulative
                ax[0].fill_between(days,
                                   deaths_lower, deaths_upper,
                                   color=color_dict[label.split('_')[0]],
                                   linestyle=line_dict[label.split('_')[1]],
                                   alpha=0.25)
                ax[0].plot(days, deaths_mean,
                           c=color_dict[label.split('_')[0]],
                           linestyle=line_dict[label.split('_')[1]], )
                ax[0].set_title(f'constant: {k}')
                ax[0].set_xlabel('Date')
                ax[0].set_ylabel('Cumulative death rate')

                # daily
                ax[1].fill_between(days[1:],
                                   d_deaths_lower, d_deaths_upper,
                                   color=color_dict[label.split('_')[0]],
                                   linestyle=line_dict[label.split('_')[1]],
                                   alpha=0.25)
                ax[1].plot(days[1:], d_deaths_mean,
                           c=color_dict[label.split('_')[0]],
                           linestyle=line_dict[label.split('_')[1]],
                           label=label.replace('model_data_', ''))
                ax[1].set_xlabel('Date')
                ax[1].set_ylabel('Daily death rates')

            ax[1].legend(loc=2)
            plt.suptitle(f'{location_name} ({model_used})')
            plt.tight_layout()
            pdf.savefig()
    return plot_path


def make_and_save_compare_average_plots(output_dir: str, raw_draw_path: str, average_draw_path: str,
                                        yesterday_draw_path: str, before_yesterday_draw_path: str) -> str:
    plotter = CompareAveragingModelDeaths(
        raw_draw_path=raw_draw_path,
        average_draw_path=average_draw_path,
        yesterday_draw_path=yesterday_draw_path,
        before_yesterday_draw_path=before_yesterday_draw_path
    )
    plot_path = f'{output_dir}/moving_average_compare.pdf'
    plotter.make_some_pictures(plot_path, 'United States of America')
    return plot_path


def make_and_save_compare_to_previous_plots(output_dir: str, today_average_path: str,
                                            previous_average_path: str) -> str:
    plotter = CompareModelDeaths(
        old_draw_path=previous_average_path,
        new_draw_path=today_average_path
    )
    plot_path = f'{output_dir}/compare_to_previous.pdf'
    plotter.make_some_pictures(plot_path, 'United States of America')
    return plot_path


def send_plots_to_diagnostics(datestamp_label: str, *plot_paths: str) -> None:
    viz_dir = Path(f'/home/j/Project/covid/results/diagnostics/deaths/{datestamp_label}/')
    if not os.path.exists(viz_dir):
        os.mkdir(viz_dir)
    for plot_path in plot_paths:
        plot_path = Path(plot_path)
        shutil.copyfile(src=plot_path, dst=viz_dir / plot_path.name)


def get_location_ids(data: pd.DataFrame) -> List[int]:
    rate_above_threshold = np.log(data[COLUMNS.death_rate]) > cmd_globals.LN_MORTALITY_RATE_THRESHOLD
    state_level = ~data[COLUMNS.state].isnull()
    location_ids = sorted(data.loc[rate_above_threshold & state_level, COLUMNS.location_id].unique().to_list())
    return location_ids


def get_us_location_ids_and_names(full_df: pd.DataFrame) -> Tuple[List[int], List[str]]:
    loc_df = get_location_metadata(location_set_id=cmd_globals.GBD_REPORTING_LOCATION_SET_ID,
                                   gbd_round_id=cmd_globals.GBD_2017_ROUND_ID)
    us_states = loc_df[COLUMNS.parent_id] == LOCATIONS.usa.id
    not_wa_state = loc_df[COLUMNS.location_name] != LOCATIONS.washington.name
    us_location_ids_except_wa = loc_df.loc[us_states & not_wa_state, COLUMNS.location_id].to_list()
    us_location_names_except_wa = loc_df.loc[us_states & not_wa_state, COLUMNS.location_name].to_list()

    wa_state_ids = []
    wa_state_names = []
    for wa_location in [LOCATIONS.other_wa_counties.name, LOCATIONS.king_and_snohomish.name, LOCATIONS.life_care.name]:
        wa_state_ids += [full_df.loc[full_df[COLUMNS.state] == wa_location, COLUMNS.location_id].unique().item()]
        wa_state_names += [wa_location]

    us_location_ids = us_location_ids_except_wa + wa_state_ids
    us_location_names = us_location_names_except_wa + wa_state_names
    return us_location_ids, us_location_names


def backcast_deaths_parallel(location_ids: List[int], death_df: pd.DataFrame,
                             age_pop_df: pd.DataFrame, age_death: pd.DataFrame) -> pd.DataFrame:
    _combiner = functools.partial(backcast_deaths,
                                  input_death_df=death_df,
                                  input_age_pop_df=age_pop_df,
                                  input_age_death_df=age_death)
    with multiprocessing.Pool(20) as p:
        backcast_deaths_dfs = p.map(_combiner, location_ids)
    return pd.concat(backcast_deaths_dfs)


def backcast_deaths(location_id: int, death_df: pd.DataFrame,
                    age_pop_df: pd.DataFrame, age_death_df: pd.DataFrame) -> pd.DataFrame:

    output_columns = [COLUMNS.location_id, COLUMNS.state, COLUMNS.country, COLUMNS.date,
                      COLUMNS.deaths, COLUMNS.death_rate, COLUMNS.population]
    death_model = DeathModelData(death_df,
                                 age_pop_df,
                                 age_death_df,
                                 location_id,
                                 'threshold',
                                 subnat=True,
                                 rate_threshold=cmd_globals.LN_MORTALITY_RATE_THRESHOLD)
    mod_df = death_model.df
    mod_df = mod_df.loc[mod_df[COLUMNS.location_id] == location_id].reset_index(drop=True)
    if len(mod_df) > 0:
        date0 = mod_df[COLUMNS.date].min()
        day0 = mod_df.loc[~mod_df[COLUMNS.date].isnull(), COLUMNS.days].min()
        mod_df.loc[mod_df[COLUMNS.days] == 0, COLUMNS.date] = date0 - timedelta(days=np.round(day0))
        mod_df = mod_df.loc[~((mod_df[COLUMNS.deaths].isnull()) & (mod_df[COLUMNS.date] == date0))]
        mod_df = mod_df.loc[~mod_df[COLUMNS.date].isnull()]
        mod_df.loc[mod_df[COLUMNS.death_rate].isnull(), COLUMNS.death_rate] = np.exp(mod_df[COLUMNS.ln_death_rate])
        mod_df.loc[mod_df[COLUMNS.deaths].isnull(), COLUMNS.deaths] = mod_df[COLUMNS.death_rate] * mod_df[COLUMNS.population]
        mod_df = mod_df.rename(index=str, columns={COLUMNS.location: COLUMNS.state})
    else:
        mod_df = pd.DataFrame(columns=output_columns)

    return mod_df[output_columns].reset_index(drop=True)


def date_mean(dates: pd.Series) -> datetime:
    dt_min = dates.min()
    deltas = [x-dt_min for x in dates]
    return dt_min + sum(deltas) / len(deltas)


def setup_ensemble_dirs(output_directory: str) -> List[str]:
    model_out_dirs = []
    for cov_sort, weights in cmd_globals.COV_SETTINGS:
        for k in cmd_globals.KS:
            # set up dirs
            model_out_dir = Path(f'{output_directory}/model_data_{cov_sort}_{k}')
            if not model_out_dir.exists():
                model_out_dir.mkdir(mode=775)
            model_out_dirs.append(str(model_out_dir))
    return model_out_dirs