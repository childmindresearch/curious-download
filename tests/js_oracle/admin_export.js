// The admin panel's export formatting, copied from mindlogger-admin/src/shared/utils
// with TypeScript types removed and logic left as-is. Used as a reference to check
// the Python port. Reads [{answer, activity, answersDecrypted}] on stdin and prints,
// for each submission, the sanitized responses.csv rows and the files the admin would
// put in its media and unity zips.
//
// Flags match a production export: enableDataExportRenaming on,
// enableSubscaleNullWhenSkipped taken from the input.

const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const flags = { enableDataExportRenaming: true, enableSubscaleNullWhenSkipped: !!input.nullWhenSkipped };
const language = 'en';

// --- shared/consts ---------------------------------------------------------
const ItemResponseType = {
  ABTrails: 'ABTrails', Audio: 'audio', AudioPlayer: 'audioPlayer', Date: 'date', Drawing: 'drawing',
  Flanker: 'flanker', Geolocation: 'geolocation', Message: 'message', MultipleSelection: 'multiSelect',
  MultipleSelectionPerRow: 'multiSelectRows', NumberSelection: 'numberSelect', ParagraphText: 'paragraphText',
  Photo: 'photo', PhrasalTemplate: 'phrasalTemplate', RequestHealthRecordData: 'requestHealthRecordData',
  SingleSelection: 'singleSelect', SingleSelectionPerRow: 'singleSelectRows', Slider: 'slider',
  SliderRows: 'sliderRows', StabilityTracker: 'stabilityTracker', Text: 'text', Time: 'time',
  TimeRange: 'timeRange', TouchPractice: 'touchPractice', TouchTest: 'touchTest', Unity: 'unity', Video: 'video',
};
const ItemsWithFileResponses = [ItemResponseType.Photo, ItemResponseType.Video, ItemResponseType.Audio, ItemResponseType.Unity];
const ActivityStatus = { Missed: 'missed', Completed: 'completed', Incomplete: 'incomplete', NotScheduled: 'not scheduled' };
const NULL_ANSWER = 'value: null';
const LookupTableItems = { Age_screen: 'age_screen', Gender_screen: 'gender_screen' };
const FinalSubscale = { Key: 'finalSubScale', FinalSubScaleScore: 'activity_score', OptionalTextForFinalSubScaleScore: 'activity_score_lookup_text' };
const LegacyFinalSubscale = { Key: 'finalSubScale', FinalSubScaleScore: 'Final SubScale Score', OptionalTextForFinalSubScaleScore: 'Optional text for Final SubScale Score' };
const SubscaleTotalScore = { Sum: 'sum', Average: 'average', Percentage: 'percentage' };
const Sex = { M: 'M', F: 'F' };
const ElementType = { Item: 'item', Subscale: 'subscale' };

// --- shared/types/guards.ts ------------------------------------------------
const isDrawingAnswerData = (item) => item.activityItem?.responseType === ItemResponseType.Drawing && Boolean(item.answer);
const isMediaAnswerData = (item) =>
  ItemsWithFileResponses.includes(item.activityItem?.responseType) && item.activityItem?.responseType !== ItemResponseType.Unity;
const isRequestHealthRecordDataAnswerData = (item) => item.activityItem?.responseType === ItemResponseType.RequestHealthRecordData;

// --- small utils -----------------------------------------------------------
const capitalize = (string) => string.charAt(0).toUpperCase() + string.slice(1);
const joinWihComma = (array, notCapitalizeItems) => {
  if (!array) return '';
  return array
    .reduce((acc, item) => {
      const stringItem = notCapitalizeItems ? String(item) : capitalize(String(item));
      if (stringItem) acc.push(stringItem);
      return acc;
    }, [])
    .join(', ');
};
const createArrayFromMinToMax = (min, max) => Array.from({ length: max - min + 1 }, (_, i) => i + min);
const getEntityKey = (item) => item.key || item.id;
const getObjectFromList = (items = [], getUniqueKey, hasIndex = false) =>
  items.reduce((acc, item, index) => ({ ...acc, [(getUniqueKey ?? getEntityKey)(item)]: { ...item, ...(hasIndex && { index }) } }), {});
const getDictionaryText = (description) => {
  if (!description) return '';
  return (typeof description === 'object' ? description[language] : description) ?? '';
};
const convertDateStampToMs = (date) => `${+date * 1000}`;

// --- getAnswerValue.ts -----------------------------------------------------
const parseValue = (value) => {
  if (value === 0) return '0';
  if ((Array.isArray(value) && !value.length) || !value) return 'null';
  return value;
};
const getAnswerValue = (answerValue) => {
  if (typeof answerValue === 'object') {
    if (answerValue?.phaseType) return answerValue;
    return parseValue(answerValue?.value ?? answerValue);
  }
  return parseValue(answerValue);
};

// --- getReportName.ts ------------------------------------------------------
const getStabilityTrackerCsvName = (item, phaseType) => `${item.targetSecretId}_${item.id}_${phaseType}.csv`;
const getABTrailsCsvName = (index, item) => `${item.targetSecretId}-${item.id || ''}-trail${index + 1}.csv`;
const getMediaFileName = (item, extension) => `${item.targetSecretId}-${item.id}-${item.activityItem.name}.${extension}`;
const getFileExtension = (fileUrl) => {
  const extension = (fileUrl.split('/').pop()?.split('.').pop() ?? '').split('?')[0] ?? '';
  if (extension === 'quicktime') return 'MOV';
  return extension;
};
const getFlankerCsvName = (item) => `${item.targetSecretId}-${item.id}-${item.activityItem.name}.csv`;

// --- getUrls.ts ------------------------------------------------------------
const getUnityMediaUrls = (item) => {
  const answer = item.answer;
  if (!answer || !answer.value) return [];
  if (Array.isArray(answer.value)) return answer.value.filter((url) => typeof url === 'string');
  if (typeof answer.value === 'string') return [];
  if ('taskData' in answer.value && answer.value.taskData?.length) return answer.value.taskData;
  return [];
};

// --- parseResponseValue.ts -------------------------------------------------
const getTimeRangeValue = (data, hasFallback = false) => {
  const hour = hasFallback ? (data?.hour ?? 0) : data?.hour;
  const minute = hasFallback ? (data?.minute ?? 0) : data?.minute;
  return `hr ${hour}, min ${minute}`;
};
const isNullAnswer = (obj) => obj === null || (typeof obj === 'object' && Object.keys(obj).length === 0);
const isTextAnswer = (answer) => typeof answer === 'string';

const parseResponseValue = (item, index, isEvent = false) => {
  let answer;
  if (!isEvent) answer = item.answer;
  const answerEdited = answer?.edited;
  const editedWithLabel = answerEdited ? ` | edited: ${answerEdited}` : '';
  const responseValue = parseResponseValueRaw(item, index, answer);
  if (answer && typeof answer === 'object' && answer.text?.length) {
    return `${responseValue !== '' ? `${responseValue} | ` : ''}text: ${answer.text}${editedWithLabel}`;
  }
  return `${responseValue}${editedWithLabel}`;
};

const parseResponseValueRaw = (item, index, answer) => {
  if (isTextAnswer(answer)) return answer;
  if (isNullAnswer(answer)) return NULL_ANSWER;
  if (isRequestHealthRecordDataAnswerData(item)) return item.ehrDataFile ?? '';

  const { activityItem } = item;
  const inputType = activityItem.responseType;
  const answerKeys = Object.keys(answer ?? {});
  const key = answer && answer === Object(answer) ? answerKeys[0] : undefined;
  const value = getAnswerValue(answer);

  if (!key || (key === 'text' && answerKeys.length < 2)) return '';
  if (isMediaAnswerData(item)) {
    try {
      if (!item.answer?.value) return '';
      return getMediaFileName(item, getFileExtension(typeof item.answer.value === 'string' ? item.answer.value : ''));
    } catch (error) {
      console.warn(error);
    }
  }

  switch (inputType) {
    case ItemResponseType.TimeRange: {
      const prefix = 'time_range: ';
      const typedValue = value;
      const from = typedValue?.from;
      const to = typedValue?.to;
      if (from === null && to === null) return `${prefix}from (empty) / to (empty)`;
      if (from === null) return `${prefix}from (empty) / to (${getTimeRangeValue(to)})`;
      if (to === null) return `${prefix}from (${getTimeRangeValue(from)}) / to (empty)`;
      return `${prefix}from (${getTimeRangeValue(from)}) / to (${getTimeRangeValue(to, true)})`;
    }
    case ItemResponseType.Date: {
      const { day, month, year } = value;
      const calculatedMonth = item?.migratedDate ? month + 1 : month;
      return `date: ${day}/${calculatedMonth}/${year}`;
    }
    case ItemResponseType.Time: {
      const timeValue = value;
      const hours = timeValue?.hours ?? answer?.hour;
      const minutes = timeValue?.minutes ?? answer?.minute;
      return `time: hr ${hours}, min ${minutes}`;
    }
    case ItemResponseType.Geolocation:
      return `geo: lat (${value?.latitude}) / long (${value?.longitude})`;
    case ItemResponseType.Drawing:
      return getMediaFileName(item, 'svg');
    case ItemResponseType.ABTrails:
      return getABTrailsCsvName(index, item);
    case ItemResponseType.SingleSelectionPerRow: {
      const { rows } = activityItem.responseValues;
      return rows.map((row, index) => `${row.rowName}: ${value[index] ?? ''}`).join('\n');
    }
    case ItemResponseType.MultipleSelectionPerRow: {
      const { rows } = activityItem.responseValues;
      return rows.map((row, index) => `${row.rowName}: ${value === 'null' ? '' : (value[index]?.join(', ') ?? '')}`).join('\n');
    }
    case ItemResponseType.SliderRows: {
      const { rows } = activityItem.responseValues;
      return rows.map((row, index) => `${row.label}: ${value[index] ?? ''}`).join('\n');
    }
    case ItemResponseType.StabilityTracker:
      return getStabilityTrackerCsvName(item, value.phaseType);
    case ItemResponseType.Flanker:
      return getFlankerCsvName(item);
    case ItemResponseType.Unity: {
      const folderName = item.id;
      const urls = getUnityMediaUrls(item);
      const filePaths = urls.map((url) => url.split('?')[0].split('/').pop()).filter(Boolean).map((fileName) => `${folderName}/${fileName}`);
      return filePaths.length ? filePaths.join(', ') : folderName;
    }
    default: {
      const correctedKey = key === 'text' && !!Object.getOwnPropertyDescriptor(answer, 'value') ? 'value' : key;
      return `${correctedKey}: ${Array.isArray(value) ? joinWihComma(value) : value}`;
    }
  }
};

// --- parseOptions.ts, getRawScores.ts, getFlag.ts --------------------------
const parseOptions = (responseValues, type) => {
  if ([ItemResponseType.SingleSelectionPerRow, ItemResponseType.MultipleSelectionPerRow, ItemResponseType.SliderRows].includes(type)) return '';
  if (type === ItemResponseType.Slider) {
    const min = Number(responseValues?.minValue);
    const max = Number(responseValues?.maxValue);
    const scores = responseValues?.scores;
    const options = createArrayFromMinToMax(min, max);
    return joinWihComma(options?.map((item, i) => `${item}: ${item}${scores?.length ? ` (score: ${scores[i]})` : ''}`) || []);
  }
  if (type === ItemResponseType.NumberSelection) {
    const min = Number(responseValues?.minValue);
    const max = Number(responseValues?.maxValue);
    return `Min: ${min}, Max: ${max}`;
  }
  if (!responseValues?.options?.length) return;
  return joinWihComma(
    responseValues.options.map(({ text, value, score }) => {
      const stringifiedValue = `${value ?? ''}`;
      return `${text}${stringifiedValue ? `: ${stringifiedValue}` : ''}${typeof score === 'number' ? ` (score: ${score})` : ''}`;
    }),
  );
};
const getRawScores = (responseValues) => {
  if (responseValues?.scores?.length) return responseValues?.scores?.reduce((acc, item) => acc + (item || 0), 0);
  return responseValues?.options?.reduce((acc, item) => acc + (item?.score || 0), 0);
};
const getFlag = (item) => {
  if (item.scheduledDatetime && !item.startDatetime) return ActivityStatus.Missed;
  const { responseType } = item.activityItem;
  switch (responseType) {
    case ItemResponseType.ABTrails: {
      const answer = item.answer;
      const isIncompleteCondition = !answer?.value || (answer.value.maximumIndex && answer.value.currentIndex !== answer.value.maximumIndex);
      if (isIncompleteCondition) return ActivityStatus.Incomplete;
      return ActivityStatus.Completed;
    }
    default:
      return ActivityStatus.Completed;
  }
};

// --- replaceItemVariableWithName.ts ----------------------------------------
const getTimeString = (obj) => {
  if (!obj) return '';
  return `${obj.hour.toString().padStart(2, '0')}:${obj.minute.toString().padStart(2, '0')}`;
};
const getDateString = (obj) => {
  if (!obj) return '';
  return `${obj.year}-${Number(obj.month).toString().padStart(2, '0')}-${Number(obj.day).toString().padStart(2, '0')}`;
};
const doubleBrackets = /\[\[(.*?)]]/g;
const getTextBetweenBrackets = (str) => {
  const listOfText = [];
  let found;
  while ((found = doubleBrackets.exec(str))) listOfText.push(found[1]);
  return listOfText;
};
const replaceItemVariableWithName = ({ markdown, items, rawAnswersObject }) => {
  try {
    const variableNames = getTextBetweenBrackets(markdown);
    if (!variableNames?.length) return markdown;
    const itemsObject = getObjectFromList(items, (item) => item.name);
    variableNames.forEach((variableName) => {
      const reg = new RegExp(`\\[\\[${variableName}\\]\\]`, 'gi');
      const itemValue = itemsObject[variableName];
      const answerValue = rawAnswersObject[variableName];
      const rawAnswer = answerValue?.answer;
      if (rawAnswer && Array.isArray(rawAnswer.value)) {
        const names = [];
        rawAnswer.value.forEach((value) => {
          const item = itemValue.responseValues.options?.find((option) => String(option.value) === String(value)) ?? null;
          if (item) names.push(item.text);
        });
        markdown = markdown.replace(reg, `${names.join(', ')} `);
      } else if (rawAnswer && typeof rawAnswer === 'object') {
        switch (itemValue.responseType) {
          case ItemResponseType.SingleSelection: {
            const item = itemValue.responseValues.options.find((option) => String(option.value) === String(rawAnswer.value));
            if (item) markdown = markdown.replace(reg, `${item.text} `);
            break;
          }
          case ItemResponseType.Slider:
          case ItemResponseType.NumberSelection:
            markdown = markdown.replace(reg, `${rawAnswer.value} `);
            break;
          case ItemResponseType.TimeRange:
            markdown = markdown.replace(reg, `${getTimeString(rawAnswer.value.from)} - ${getTimeString(rawAnswer.value.to)} `);
            break;
          case ItemResponseType.Date:
            markdown = markdown.replace(reg, `${getDateString(rawAnswer.value)} `);
            break;
        }
      } else if (rawAnswer) {
        markdown = markdown.replace(reg, rawAnswer.toString().replace(/(?=[$&])/g, '\\'));
      }
      markdown = markdown.replace(reg, ' ');
    });
  } catch (error) {
    // console.warn(error);
  }
  return markdown;
};

// --- getSubscales.ts -------------------------------------------------------
const isSystemItem = (item) => !item.allowEdit && (item.name === LookupTableItems.Age_screen || item.name === LookupTableItems.Gender_screen);
const getRoundTo2Decimal = (num) => Math.round((num + Number.EPSILON) * 100) / 100;
const getSubscaleScore = (subscalesSum, type, length, maxScore) => {
  if (subscalesSum === null) return null;
  switch (type) {
    case SubscaleTotalScore.Sum: return subscalesSum;
    case SubscaleTotalScore.Average: return length === 0 ? 0 : getRoundTo2Decimal(subscalesSum / length);
    case SubscaleTotalScore.Percentage: return maxScore === 0 ? 0 : getRoundTo2Decimal((subscalesSum * 100) / maxScore);
    default: return 0;
  }
};
const parseSex = (sex) => (sex === Sex.M ? '0' : '1');
const INTERVAL_SYMBOL = '~';
const calcScores = (data, activityItems, subscalesObject, flags, result = {}) => {
  let itemCount = 0;
  let maxScore = 0;
  const defaultScore = flags.enableSubscaleNullWhenSkipped ? null : 0;
  const sumScore = data.items.reduce((acc, item) => {
    if (!item.type) return acc;
    if (item.type === ElementType.Subscale) {
      itemCount++;
      const calculatedNestedSubscale = calcScores(subscalesObject[item.name], activityItems, subscalesObject, flags, result)[item.name];
      if (typeof calculatedNestedSubscale?.score === 'number') {
        result[item.name] = calculatedNestedSubscale;
        return (acc ?? 0) + calculatedNestedSubscale.score;
      }
      return acc;
    }
    const activityItem = activityItems[item.name];
    if (isSystemItem(item) || activityItem?.activityItem.isHidden) return acc;
    const answer = activityItem?.answer;
    const typedOptions = activityItem?.activityItem.responseValues;
    let value = null;
    if (typedOptions) {
      if ('options' in typedOptions && typedOptions.options.length) {
        const scoresObject = typedOptions.options.reduce((acc, item) => {
          if (item.value !== undefined && item.score !== undefined) acc[item.value] = item.score;
          return acc;
        }, {});
        if ('type' in typedOptions && typedOptions.type === 'singleSelect') {
          maxScore += Math.max(...Object.values(scoresObject));
        } else {
          maxScore += Object.values(scoresObject).reduce((acc, result) => acc + result, 0);
        }
        if (Array.isArray(answer?.value)) {
          value = answer?.value.reduce((result, val) => {
            if (scoresObject[val] === null) return result;
            return (result ?? 0) + scoresObject[val];
          }, null) ?? null;
        } else {
          value = (answer && scoresObject[answer.value]) ?? null;
        }
      } else if ('scores' in typedOptions && typedOptions.scores?.length) {
        const min = Number(typedOptions.minValue);
        const max = Number(typedOptions.maxValue);
        const scores = typedOptions.scores;
        const options = createArrayFromMinToMax(min, max);
        maxScore += Math.max(...scores);
        value = scores[options.findIndex((item) => item === answer?.value)] ?? null;
      }
    }
    if (value === null) {
      if (flags.enableSubscaleNullWhenSkipped) return acc;
      value = 0;
    }
    itemCount++;
    return (acc ?? 0) + value;
  }, defaultScore);

  const calculatedScore = getSubscaleScore(sumScore, data.scoring, itemCount, maxScore);
  if (calculatedScore !== null && data?.subscaleTableData) {
    const subscaleTableDataItem = data.subscaleTableData?.find(({ sex, age, rawScore }) => {
      const genderAnswer = activityItems[LookupTableItems.Gender_screen]?.answer;
      const withSex = !sex || parseSex(sex) === String(genderAnswer?.value);
      const ageAnswer = activityItems[LookupTableItems.Age_screen]?.answer;
      let reportedAge;
      if (ageAnswer) {
        if (typeof ageAnswer === 'string') reportedAge = ageAnswer;
        else if ('value' in ageAnswer && ['number', 'string'].includes(typeof ageAnswer.value)) reportedAge = String(ageAnswer.value);
      }
      const hasAgeInterval = age && typeof age === 'string' && age.includes(INTERVAL_SYMBOL);
      let withAge = true;
      if (age) {
        if (!hasAgeInterval) {
          withAge = String(age) === reportedAge;
        } else {
          const [minAge, maxAge] = age.replace(/\s/g, '').split(INTERVAL_SYMBOL);
          const reportedAgeNum = Number(reportedAge);
          withAge = Number(minAge) <= reportedAgeNum && reportedAgeNum <= Number(maxAge);
        }
      }
      if (!withSex || !withAge) return false;
      const hasInterval = rawScore.includes(INTERVAL_SYMBOL);
      if (!hasInterval) return rawScore === String(calculatedScore);
      const [minScore, maxScore] = rawScore.replace(/\s/g, '').split(INTERVAL_SYMBOL);
      return Number(minScore) <= calculatedScore && calculatedScore <= Number(maxScore);
    });
    return {
      ...result,
      [data.name]: {
        score: Number(subscaleTableDataItem?.score) || getRoundTo2Decimal(calculatedScore),
        optionText: subscaleTableDataItem?.optionalText || '',
        severity: subscaleTableDataItem?.severity || null,
      },
    };
  }
  return {
    ...result,
    ...(typeof calculatedScore === 'number' && { [data.name]: { score: getRoundTo2Decimal(calculatedScore), optionText: '', severity: null } }),
  };
};
const calcTotalScore = (subscaleSetting, activityItems, flags) => {
  if (!subscaleSetting?.calculateTotalScore) return {};
  return calcScores(
    {
      name: flags.enableDataExportRenaming ? FinalSubscale.Key : LegacyFinalSubscale.Key,
      items: Object.keys(activityItems).reduce((acc, item) => {
        const itemType = activityItems[item].activityItem.responseType;
        const allowEdit = activityItems[item].activityItem.allowEdit;
        if (itemType === ItemResponseType.SingleSelection || itemType === ItemResponseType.MultipleSelection || itemType === ItemResponseType.Slider) {
          acc.push({ name: item, type: ElementType.Item, allowEdit });
        }
        return acc;
      }, []),
      scoring: subscaleSetting.calculateTotalScore,
      subscaleTableData: subscaleSetting.totalScoresTableData,
    },
    activityItems,
    {},
    flags,
  );
};
const getSubscales = (subscaleSetting, activityItems, flags) => {
  if (!subscaleSetting?.subscales?.length || !Object.keys(activityItems).length) return {};
  const subscalesObject = getObjectFromList(subscaleSetting.subscales, (item) => item.name);
  const cleanName = (name) => name.replace(/[^a-zA-Z0-9-]/g, '_');
  const parsedSubscales = subscaleSetting.subscales.reduce((acc, item) => {
    const calculatedSubscale = calcScores(item, activityItems, subscalesObject, flags)[item.name];
    if (!calculatedSubscale) return acc;
    const cleanedName = cleanName(item.name);
    if (flags.enableDataExportRenaming) {
      acc[`subscale_name_${cleanedName}`] = calculatedSubscale?.score;
      if (calculatedSubscale?.optionText) acc[`subscale_lookup_text_${cleanedName}`] = calculatedSubscale.optionText;
    } else {
      acc[item.name] = calculatedSubscale?.score;
      if (calculatedSubscale?.optionText) acc[`Optional text for ${item.name}`] = calculatedSubscale.optionText;
    }
    return acc;
  }, {});
  const result = subscaleSetting.calculateTotalScore && calcTotalScore(subscaleSetting, activityItems, flags);
  const finalSubscale = flags.enableDataExportRenaming ? FinalSubscale : LegacyFinalSubscale;
  const calculatedTotalScore = result?.[finalSubscale.Key];
  return {
    ...(typeof calculatedTotalScore?.score === 'number' && {
      [finalSubscale.FinalSubScaleScore]: calculatedTotalScore.score,
      [finalSubscale.OptionalTextForFinalSubScaleScore]: calculatedTotalScore.optionText,
    }),
    ...parsedSubscales,
  };
};

// --- getReportCSVObject.ts (renamed branch) --------------------------------
const getReportCSVObject = ({ item, rawAnswersObject, index }) => {
  const {
    activityItem, scheduledDatetime, startDatetime, endDatetime, respondentSecretId, respondentId,
    sourceSubjectId, sourceSecretId, sourceUserNickname, sourceUserTag, targetSubjectId, targetSecretId,
    targetUserNickname, targetUserTag, inputSubjectId, inputSecretId, inputUserNickname, relation,
    activityId, activityName, flowId, flowName, version, reviewedAnswerId, reviewedFlowSubmitId,
    legacyProfileId, scheduledEventId, scheduledEventHistoryId, tzOffset, submitId,
  } = item;
  const responseValues = activityItem?.responseValues;
  return {
    target_id: targetSubjectId ?? '',
    target_secret_id: targetSecretId ?? '',
    target_nickname: targetUserNickname ?? '',
    target_tag: targetUserTag ?? '',
    source_id: sourceSubjectId ?? '',
    source_secret_id: sourceSecretId ?? '',
    source_nickname: sourceUserNickname ?? '',
    source_tag: sourceUserTag ?? '',
    source_relation: relation ?? '',
    input_id: inputSubjectId ?? '',
    input_secret_id: inputSecretId ?? '',
    input_nickname: inputUserNickname ?? '',
    userId: respondentId ?? '',
    secret_user_id: respondentSecretId ?? '',
    legacy_user_id: legacyProfileId ?? '',
    applet_version: version ?? '',
    activity_flow_id: flowId ?? '',
    activity_flow_name: flowName ?? '',
    activity_flow_submission_id: flowId && !reviewedFlowSubmitId ? submitId : '',
    activity_id: activityId,
    activity_name: activityName,
    activity_submission_id: item.id,
    activity_start_time: convertDateStampToMs(startDatetime),
    activity_end_time: convertDateStampToMs(endDatetime),
    activity_schedule_id: scheduledEventId ?? '',
    activity_schedule_history_id: scheduledEventHistoryId ?? '',
    activity_schedule_start_time: scheduledDatetime ? convertDateStampToMs(scheduledDatetime) : ActivityStatus.NotScheduled,
    utc_timezone_offset: tzOffset ?? '',
    activity_submission_review_id: reviewedFlowSubmitId ?? reviewedAnswerId ?? '',
    item_id: activityItem.id ?? '',
    item_name: activityItem.name,
    item_prompt: replaceItemVariableWithName({ markdown: getDictionaryText(activityItem.question), items: item.items, rawAnswersObject }),
    item_response_options: replaceItemVariableWithName({
      markdown: parseOptions(responseValues, item.activityItem?.responseType) ?? '',
      items: item.items,
      rawAnswersObject,
    }),
    item_response: parseResponseValue(item, index),
    item_response_status: getFlag(item),
    item_type: activityItem.responseType,
    rawScore: getRawScores(responseValues) || '',
  };
};

// --- getDecryptedAnswers.ts + getParsedAnswers.ts + getReportAndMediaData.ts
const getDecryptedAnswers = (answersApiResponse, answersDecrypted) => {
  const { userPublicKey, answer, itemIds: _itemIds, events, migratedData, ...rest } = answersApiResponse;
  const migratedUrls = migratedData?.decryptedFileAnswers && getObjectFromList(migratedData.decryptedFileAnswers, (item) => item.answerItemId);
  const getAnswer = (activityItem, index) => {
    const answer = answersDecrypted[index];
    if (!migratedUrls) return answer;
    const migratedUrl = migratedUrls[activityItem?.id];
    if (migratedUrl && typeof answer === 'object') {
      if (activityItem.responseType === ItemResponseType.Drawing) {
        return { ...answer, value: { ...answer.value, uri: migratedUrl?.fileUrl } };
      }
      return { ...answer, value: migratedUrl.fileUrl };
    }
    return answer;
  };
  return rest.items.reduce((acc, activityItem, index) => {
    if (activityItem.isHidden) return acc;
    return acc.concat({ activityItem, answer: getAnswer(activityItem, index), ...rest });
  }, []);
};
const remapFailed = (decryptedAnswers) =>
  decryptedAnswers.map((item) => {
    if (typeof item.answer === 'object' && item.answer !== null && 'type' in item.answer && 'screen' in item.answer && 'time' in item.answer) {
      return { ...item, answer: null };
    }
    return item;
  });
// --- getUrls.ts, getParsedAnswers.ts getAnswersWithPublicUrls ---------------
// Two deliberate changes: presigning is the identity (it keeps the file path, which is
// all the formatting looks at), and a drawing without an uploaded file is reported as
// `inline:<svg>` instead of a browser blob URL.
const isUnityAnswerData = (item) => item.activityItem?.responseType === ItemResponseType.Unity;
const isNotMediaAnswerData = (item) => !ItemsWithFileResponses.includes(item.activityItem?.responseType) || !item.answer;
const getDrawingUrl = (item) => {
  const drawingAnswer = item.answer;
  if (drawingAnswer.value.uri) return drawingAnswer.value.uri;
  return `inline:${drawingAnswer.value.svgString}`;
};
const getMediaUrl = (item) => {
  const answer = item.answer;
  if (!answer) return '';
  if (typeof answer.value === 'string') {
    return answer.value;
  } else if (Array.isArray(answer.value)) {
    return answer.value[0];
  } else if (answer.value && typeof answer.value === 'object' && 'uri' in answer.value) {
    return answer.value.uri || '';
  }
  return '';
};
const shouldConvertPrivateDrawingUrl = (item) => isDrawingAnswerData(item) && Boolean(item.answer.value.uri);
const getAnswersWithPublicUrls = (parsedAnswers) => {
  if (!parsedAnswers.length) return [];
  const privateUrls = parsedAnswers.reduce((acc, data) => {
    const decryptedAnswers = data.decryptedAnswers.reduce((urlsAcc, item) => {
      if (shouldConvertPrivateDrawingUrl(item)) return urlsAcc.concat(getDrawingUrl(item));
      if (!item.answer) return urlsAcc;
      if (isMediaAnswerData(item)) {
        return urlsAcc.concat(getMediaUrl(item));
      } else if (isUnityAnswerData(item)) {
        const unityUrls = getUnityMediaUrls(item);
        if (unityUrls.length) return urlsAcc.concat(...unityUrls);
        return urlsAcc.concat(getMediaUrl(item));
      } else {
        return urlsAcc;
      }
    }, []);
    return acc.concat(decryptedAnswers);
  }, []);
  const publicUrls = privateUrls; // presign: identity
  let publicUrlIndex = 0;
  return parsedAnswers.reduce((acc, data) => {
    const decryptedAnswers = data.decryptedAnswers.reduce((decryptedAnswersAcc, item) => {
      if (shouldConvertPrivateDrawingUrl(item)) {
        return decryptedAnswersAcc.concat({
          ...item,
          answer: { ...item.answer, value: { ...item.answer.value, uri: publicUrls[publicUrlIndex++] ?? '' } },
        });
      }
      if (!item.answer) return decryptedAnswersAcc.concat(item);
      if (isUnityAnswerData(item)) {
        const originalUrls = getUnityMediaUrls(item);
        if (originalUrls.length) {
          const publicTaskUrls = originalUrls.map(() => publicUrls[publicUrlIndex++] ?? '');
          return decryptedAnswersAcc.concat({ ...item, answer: { ...item.answer, value: { taskData: publicTaskUrls } } });
        }
        return decryptedAnswersAcc.concat({ ...item, answer: { ...item.answer, value: publicUrls[publicUrlIndex++] ?? '' } });
      }
      if (isNotMediaAnswerData(item)) return decryptedAnswersAcc.concat(item);
      return decryptedAnswersAcc.concat({ ...item, answer: { ...item.answer, value: publicUrls[publicUrlIndex++] ?? '' } });
    }, []);
    return acc.concat({ ...data, decryptedAnswers });
  }, []);
};

// --- getReportAndMediaData.ts getMediaData / getUnityData -------------------
const getMediaData = (mediaData, decryptedAnswers) => {
  const mediaAnswers = decryptedAnswers.reduce((filteredAcc, item) => {
    if (isDrawingAnswerData(item)) return filteredAcc.concat({ fileName: getMediaFileName(item, 'svg'), url: getDrawingUrl(item) });
    const responseType = item.activityItem?.responseType;
    const url = getMediaUrl(item);
    if (!ItemsWithFileResponses.includes(responseType) || !url) return filteredAcc;
    return filteredAcc.concat({ fileName: getMediaFileName(item, getFileExtension(url)), url });
  }, []);
  return mediaData.concat(...mediaAnswers);
};
const getUnityData = (unityData, decryptedAnswers) => {
  const unityAnswers = decryptedAnswers.reduce((filteredAcc, item) => {
    const responseType = item.activityItem?.responseType;
    if (responseType !== ItemResponseType.Unity) return filteredAcc;
    const folderName = item.id;
    const mediaData = getUnityMediaUrls(item).map((url, index) => {
      const urlFileName = url.split('?')[0].split('/').pop() ?? '';
      return { fileName: `${folderName}/${urlFileName || `${index}.${getFileExtension(url)}`}`, url };
    });
    return filteredAcc.concat(mediaData);
  }, []);
  return unityData.concat(...unityAnswers);
};

const getReportData = (rawAnswersObject, decryptedAnswers) => {
  const answers = decryptedAnswers.reduce((filteredAcc, item, index) => {
    const shouldSkipItem = item.answer === undefined || item.answer === null;
    if (shouldSkipItem) return filteredAcc;
    return filteredAcc.concat(getReportCSVObject({ item, rawAnswersObject, index }));
  }, []);
  const subscaleSetting = decryptedAnswers?.[0]?.subscaleSetting;
  if (subscaleSetting?.subscales?.length) {
    answers.splice(0, 1, { ...answers[0], ...getSubscales(subscaleSetting, rawAnswersObject, flags) });
  }
  return answers;
};

// --- csvSanitization.ts ----------------------------------------------------
const DANGEROUS_CSV_CHARS = /^[=+\-@\t\r\n]/;
function sanitizeCSVValue(value) {
  if (value === null || value === undefined) return '';
  if (Array.isArray(value)) return value.map((item) => String(item === null || item === undefined ? '' : item)).join(',');
  const stringValue = String(value);
  if (stringValue.length === 0) return stringValue;
  if (typeof value === 'number' && value < 0) return stringValue;
  const lines = stringValue.split(/\r\n|\r|\n/);
  return lines.map((line) => (DANGEROUS_CSV_CHARS.test(line) ? `'${line}` : line)).join('\n');
}
const sanitizeRow = (row) => Object.fromEntries(Object.entries(row).map(([key, value]) => [key, sanitizeCSVValue(value)]));

// --- run (prepareDecryptedData order) ---------------------------------------
const output = input.submissions.map(({ answer, activity, answersDecrypted }) => {
  const withActivity = { ...answer, items: activity.items, activityName: activity.name, subscaleSetting: activity.subscaleSetting };
  const remapped = remapFailed(getDecryptedAnswers(withActivity, answersDecrypted));
  const [{ decryptedAnswers: decrypted }] = getAnswersWithPublicUrls([{ decryptedAnswers: remapped }]);
  const rawAnswersObject = getObjectFromList(decrypted, (item) => item.activityItem.name);
  return {
    rows: getReportData(rawAnswersObject, decrypted).map(sanitizeRow),
    media: getMediaData([], decrypted),
    unity: getUnityData([], decrypted),
  };
});
process.stdout.write(JSON.stringify(output));
